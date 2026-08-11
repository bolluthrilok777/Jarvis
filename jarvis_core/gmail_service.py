"""Gmail API service for reading emails, downloading attachments, and creating drafts.

Uses Gmail API OAuth 2.0. Falls back gracefully if credentials are missing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from pathlib import Path
from typing import Any, Optional

import requests

from jarvis_core.gmail_models import GmailMessagePreview

logger = logging.getLogger("jarvis.gmail_service")

# Paths for credentials, token, and OAuth state
CONFIG_DIR = Path("C:/Jarvis/config")
DATA_DIR = Path("C:/Jarvis/data")
CREDENTIALS_FILE = CONFIG_DIR / "gmail_credentials.json"
TOKEN_FILE = CONFIG_DIR / "gmail_token.json"
OAUTH_STATE_FILE = DATA_DIR / "gmail_oauth_state.json"

SCOPES_READ_ONLY = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

SCOPES_WITH_SEND = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Global service cache
_gmail_service = None
_credentials_available = False
# Set True when Google returns invalid_grant (token revoked/expired permanently)
# Cleared when a new token is saved after re-authorization.
_token_revoked = False

EXPECTED_REDIRECT_URI = "http://127.0.0.1:8000/gmail/oauth/callback"


def credentials_exist() -> bool:
    """Check if Gmail credentials file exists."""
    return CREDENTIALS_FILE.exists()


def token_exists() -> bool:
    """Check if Gmail token file exists."""
    return TOKEN_FILE.exists()


def is_connected() -> bool:
    """Check if Gmail API is connected (token + credentials available)."""
    return credentials_exist() and token_exists()


def _get_credentials() -> Any:
    """Load credentials from file. Returns None if not available."""
    try:
        if not CREDENTIALS_FILE.exists():
            return None
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            creds_data = json.load(f)
        return creds_data
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Gmail credentials JSON: %s", e)
        return None
    except Exception as e:
        logger.error("Failed to load Gmail credentials: %s", e)
        return None


def _get_token() -> Any:
    """Load token from file. Returns None if not available."""
    try:
        if not TOKEN_FILE.exists():
            return None
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load Gmail token: %s", e)
        return None


def _build_service() -> Any:
    """Build Gmail API service using requests transport (bypasses httplib2).

    Fixes [SSL: WRONG_VERSION_NUMBER] on Windows by using urllib3/requests
    instead of httplib2 for all Gmail API HTTP calls.

    Never caches the service — always builds fresh per call.
    """
    global _gmail_service, _credentials_available

    if not is_connected():
        _credentials_available = False
        return None

    try:
        from google.auth.transport.requests import Request, AuthorizedSession
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_data = _get_token()
        if token_data is None:
            return None

        creds = Credentials.from_authorized_user_info(token_data, SCOPES_READ_ONLY)

        # Refresh expired token using requests transport (not httplib2)
        if creds and creds.expired and creds.refresh_token:
            try:
                fresh_session = requests.Session()
                creds.refresh(Request(session=fresh_session))
                # Save refreshed token
                refreshed_json = creds.to_json()
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    f.write(refreshed_json)
                logger.info("Gmail token refreshed successfully")
                # Reload from saved JSON to get a clean, valid Credentials object.
                # The in-memory creds object may have stale internal state after
                # refresh (expiry not propagated), causing creds.valid = False.
                creds = Credentials.from_authorized_user_info(
                    json.loads(refreshed_json), SCOPES_READ_ONLY
                )
            except Exception as e:
                err_str = str(e).lower()
                if "invalid_grant" in err_str or "token has been expired or revoked" in err_str:
                    global _token_revoked
                    _token_revoked = True
                    # Delete the dead token file so is_connected() returns False
                    # and the monitor stops trying. User must re-authorize.
                    try:
                        if TOKEN_FILE.exists():
                            TOKEN_FILE.unlink()
                    except Exception:
                        pass
                    # Write a marker file so the UI shows the reconnect banner
                    # even after Jarvis is restarted
                    try:
                        marker = CONFIG_DIR / "gmail_token_revoked.marker"
                        marker.write_text("revoked")
                    except Exception:
                        pass
                    logger.warning(
                        "Gmail token permanently revoked (invalid_grant). "
                        "Token file deleted. Please reconnect Gmail via the UI."
                    )
                else:
                    logger.warning("Could not refresh Gmail token: %s", e)
                return None

        if not creds or not creds.valid:
            # Log why credentials are invalid for debugging (safe, no secrets)
            if creds:
                logger.warning(
                    "Gmail credentials are not valid: token=%s expired=%s has_token=%s",
                    "set" if creds.token else "missing",
                    creds.expired,
                    bool(creds.token),
                )
            else:
                logger.warning("Gmail credentials are not valid: creds is None")
            return None

        # Use AuthorizedSession (requests/urllib3) instead of httplib2.
        # httplib2 causes [SSL: WRONG_VERSION_NUMBER] on Windows.
        # AuthorizedSession wraps requests.Session with auto token refresh.
        authed_session = AuthorizedSession(creds)

        # Adapter class: makes AuthorizedSession look like httplib2 to googleapiclient.
        # This completely bypasses httplib2 for all Gmail API HTTP calls, fixing
        # [SSL: WRONG_VERSION_NUMBER] on Windows where httplib2's TLS negotiation fails.
        class _RequestsHttpAdapter:
            """Adapts google.auth.transport.requests.AuthorizedSession
            to the httplib2-like interface expected by googleapiclient.
            Fixes [SSL: WRONG_VERSION_NUMBER] on Windows.
            """

            def __init__(self, session: AuthorizedSession):
                self._session = session
                self.connections = {}  # googleapiclient may check this attribute

            def request(
                self,
                uri: str,
                method: str = "GET",
                body: Any = None,
                headers: Any = None,
                redirections: int = 5,
                connection_type: Any = None,
                **kwargs,
            ):
                """Make HTTP request via requests, return (httplib2-like Response, content)."""
                resp = self._session.request(
                    method=method,
                    url=uri,
                    data=body,
                    headers=headers or {},
                    timeout=30,
                    allow_redirects=(redirections > 0),
                )

                # Build httplib2-compatible response object
                class _HttpResponse:
                    def __init__(self, r):
                        self.status = r.status_code
                        self.reason = r.reason
                        self._headers = {k.lower(): v for k, v in r.headers.items()}

                    def __getitem__(self, key):
                        return self._headers.get(key.lower(), "")

                    def get(self, key, default=None):
                        return self._headers.get(key.lower(), default)

                    def items(self):
                        return self._headers.items()

                    def __contains__(self, key):
                        return key.lower() in self._headers

                content = resp.content
                return _HttpResponse(resp), content

        http_adapter = _RequestsHttpAdapter(authed_session)
        service = build("gmail", "v1", http=http_adapter, cache_discovery=False)
        _credentials_available = True
        logger.debug("Gmail API service built (requests/urllib3, no httplib2)")
        return service

    except ImportError:
        logger.warning(
            "Google API libraries not installed. "
            "Install with: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
        )
        return None
    except Exception as e:
        logger.error("Failed to build Gmail service: %s", e)
        return None


def reset_service() -> None:
    """Reset the cached Gmail service (e.g., after token refresh)."""
    global _gmail_service, _credentials_available
    _gmail_service = None
    _credentials_available = False


def mark_token_fresh() -> None:
    """Clear the revoked flag when a new token is saved after re-authorization."""
    global _token_revoked
    _token_revoked = False
    # Delete marker file so the reconnect banner disappears after fresh login
    try:
        marker = CONFIG_DIR / "gmail_token_revoked.marker"
        if marker.exists():
            marker.unlink()
    except Exception:
        pass


def is_token_revoked() -> bool:
    """Return True if token was revoked (checks in-memory flag + marker file)."""
    if _token_revoked:
        return True
    # Also check marker file — persists across restarts
    return (CONFIG_DIR / "gmail_token_revoked.marker").exists()


def validate_token_health() -> dict:
    """Check token file health and return a safe status dict.

    Returns:
    {
        "valid": bool,
        "has_token": bool,
        "has_refresh_token": bool,
        "expired": bool,
        "error": str or None
    }
    Never exposes token values or secrets.
    """
    if not TOKEN_FILE.exists():
        return {"valid": False, "has_token": False, "has_refresh_token": False,
                "expired": False, "error": "Token file not found"}
    try:
        from google.oauth2.credentials import Credentials
        token_data = _get_token()
        if not token_data:
            return {"valid": False, "has_token": False, "has_refresh_token": False,
                    "expired": False, "error": "Token file empty or unreadable"}
        creds = Credentials.from_authorized_user_info(token_data, SCOPES_READ_ONLY)
        return {
            "valid": creds.valid,
            "has_token": bool(creds.token),
            "has_refresh_token": bool(creds.refresh_token),
            "expired": creds.expired,
            "revoked": _token_revoked,
            "error": None,
        }
    except Exception as e:
        return {"valid": False, "has_token": False, "has_refresh_token": False,
                "expired": False, "revoked": _token_revoked, "error": str(e)[:100]}


# ── Direct OAuth2 Web Flow Methods (New Implementation) ─────────────────

def validate_web_credentials_strict(data: dict) -> tuple[bool, str]:
    """Validate credentials for Web OAuth only (strict validation).
    
    Returns (is_valid, error_message).
    Only accepts Web application OAuth credentials.
    """
    if not isinstance(data, dict):
        return False, "Invalid JSON: must be an object"

    # Only accept Web application credentials
    if "web" not in data:
        if "installed" in data:
            return False, "Wrong OAuth credential type. Upload Web application OAuth JSON."
        return False, "Missing 'web' OAuth client info"

    web = data["web"]
    if not isinstance(web, dict):
        return False, "Web client info must be an object"

    # Validate required fields
    required_fields = ["client_id", "client_secret", "auth_uri", "token_uri"]
    for field in required_fields:
        if field not in web:
            return False, f"Missing '{field}' in web credentials"
        if not web[field]:
            return False, f"'{field}' is empty"

    # Validate redirect URIs
    redirect_uris = web.get("redirect_uris", [])
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return False, "Missing 'redirect_uris' array in web credentials"
    
    if EXPECTED_REDIRECT_URI not in redirect_uris:
        return False, f"Redirect URI {EXPECTED_REDIRECT_URI} not found in web credentials"

    # Validate JavaScript origins
    javascript_origins = web.get("javascript_origins", [])
    if not isinstance(javascript_origins, list) or not javascript_origins:
        return False, "Missing 'javascript_origins' array in web credentials"
    
    if "http://127.0.0.1:8000" not in javascript_origins:
        return False, "JavaScript origin http://127.0.0.1:8000 not found in web credentials"

    return True, ""


def get_authorization_url_direct() -> tuple[Optional[str], Optional[str]]:
    """Generate Google OAuth authorization URL using direct OAuth2 web flow.
    
    Returns (url, error_message) tuple.
    """
    creds_data = _get_credentials()
    if creds_data is None:
        return (None, "Gmail OAuth credentials not found or invalid JSON")

    # Only accept Web credentials
    if "web" not in creds_data:
        if "installed" in creds_data:
            return (None, "Wrong OAuth credential type. Upload Web application OAuth JSON.")
        return (None, "Invalid credentials format. Must contain 'web' key.")

    web = creds_data["web"]
    
    # Validate credentials strictly
    is_valid, error = validate_web_credentials_strict(creds_data)
    if not is_valid:
        return (None, error)

    try:
        # Generate fresh state token
        state = secrets.token_urlsafe(32)
        
        # Save state temporarily
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(OAUTH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"state": state}, f)
        
        # Build authorization URL manually - ensure scope is properly formatted
        base_url = "https://accounts.google.com/o/oauth2/v2/auth"
        scope_string = " ".join(SCOPES_READ_ONLY)
        
        params = {
            "client_id": web["client_id"],
            "redirect_uri": EXPECTED_REDIRECT_URI,
            "response_type": "code",
            "scope": scope_string,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        
        # Use urlencode to properly encode all parameters
        auth_url = f"{base_url}?{urllib.parse.urlencode(params)}"
        
        # Safe debug logging
        logger.info("OAuth authorization URL generated with params: client_id=yes redirect_uri=yes scope_count=%d state=yes", len(SCOPES_READ_ONLY))
        
        return (auth_url, None)

    except Exception as e:
        logger.error("Failed to generate authorization URL: %s", e)
        return (None, f"Failed to generate authorization URL: {str(e)[:200]}")


def exchange_code_for_token_direct(code: str, state: str = None) -> tuple[bool, str]:
    """Exchange OAuth code for token using direct HTTP requests.
    
    Returns (success, message_or_email).
    """
    try:
        creds_data = _get_credentials()
        if creds_data is None:
            return False, "Credentials not found"

        if "web" not in creds_data:
            return False, "Invalid credentials format"

        web = creds_data["web"]

        # Verify state if provided
        if state is not None:
            try:
                if OAUTH_STATE_FILE.exists():
                    with open(OAUTH_STATE_FILE, "r", encoding="utf-8") as f:
                        state_data = json.load(f)
                    expected_state = state_data.get("state")
                    if state != expected_state:
                        logger.warning("OAuth state mismatch")
                        return False, "State mismatch. Please try connecting again."
                    # Clean up state file after use
                    OAUTH_STATE_FILE.unlink()
                else:
                    logger.warning("OAuth state file missing")
                    return False, "State verification failed. Please try connecting again."
            except Exception as e:
                logger.warning("OAuth state verification failed: %s", e)
                return False, "State verification failed. Please try connecting again."

        # Exchange code for token using direct HTTP request
        token_url = web["token_uri"]
        token_data = {
            "code": code,
            "client_id": web["client_id"],
            "client_secret": web["client_secret"],
            "redirect_uri": EXPECTED_REDIRECT_URI,
            "grant_type": "authorization_code",
        }

        logger.info("Token exchange HTTP request started")
        response = requests.post(token_url, data=token_data, timeout=30)
        
        logger.info("Token exchange HTTP status: %s", response.status_code)
        
        if response.status_code != 200:
            # Read safe error information
            try:
                error_data = response.json()
                error_code = error_data.get("error", "unknown_error")
                error_description = error_data.get("error_description", "Token exchange failed")
                logger.warning("Token exchange failed: %s", error_code)
                
                if error_code == "invalid_grant":
                    return False, "Authorization code expired or already used. Please try connecting again."
                elif error_code == "invalid_client":
                    return False, "Invalid OAuth credentials. Please check your Web application configuration."
                else:
                    return False, f"Token exchange failed: {error_description[:100]}"
            except:
                return False, "Token exchange failed with HTTP error"

        # Parse successful token response
        token_response = response.json()
        
        # Build token data compatible with google-auth Credentials
        token_data = {
            "token": token_response.get("access_token"),  # Map access_token to token
            "refresh_token": token_response.get("refresh_token"),
            "token_uri": web["token_uri"],
            "client_id": web["client_id"],
            "client_secret": web["client_secret"],
            "scopes": SCOPES_READ_ONLY,
        }
        
        # Add expiry if available
        if "expires_in" in token_response:
            from datetime import datetime, timedelta
            expiry = datetime.utcnow() + timedelta(seconds=int(token_response["expires_in"]))
            token_data["expiry"] = expiry.isoformat() + "Z"

        # Save token
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(token_data, f, indent=2)

        logger.info("Token saved: yes")
        reset_service()
        mark_token_fresh()

        # Try to get account email
        email = get_account_email()
        if email:
            logger.info("Gmail profile fetch: success")
            return True, email
        logger.info("Gmail profile fetch: failure (no email returned)")
        return True, "Token saved (email unknown)"

    except requests.RequestException as e:
        logger.error("HTTP request failed during token exchange: %s", e)
        return False, "Network error during token exchange. Please try again."
    except Exception as e:
        logger.error("Failed to exchange code for token: %s", e)
        return False, f"Token exchange failed: {str(e)[:200]}"


# ── OAuth Flow Methods ────────────────────────────────────────────────────


def _is_web_credentials(data: dict) -> bool:
    """Check if credentials are Web application type."""
    return "web" in data and isinstance(data["web"], dict)


def _is_installed_credentials(data: dict) -> bool:
    """Check if credentials are Desktop (installed) application type."""
    return "installed" in data and isinstance(data["installed"], dict)


# Remove these old functions as we now use direct OAuth2 flow
# def _is_web_credentials, _is_installed_credentials, _get_credentials_type, _validate_web_credentials_for_auth


def get_credentials_error() -> Optional[str]:
    """Check if there's a credential configuration error.
    
    Returns None if credentials are valid, or error message if invalid.
    """
    if not credentials_exist():
        return None  # No error if no file exists
        
    creds_data = _get_credentials()
    if creds_data is None:
        return "Invalid credentials JSON format"
        
    # Only accept Web credentials
    if "web" not in creds_data:
        if "installed" in creds_data:
            return "Wrong OAuth credential type. Upload Web application OAuth JSON."
        return "Invalid credentials format. Must contain 'web' key"
        
    # Validate Web credentials
    is_valid, error = validate_credentials_json(creds_data)
    if not is_valid:
        return error
        
    return None  # No errors


def _get_credentials_type(data: dict) -> str:
    """Return 'web', 'installed', or 'unknown'."""
    if _is_web_credentials(data):
        return "web"
    if _is_installed_credentials(data):
        return "installed"
    return "unknown"


def validate_credentials_json(data: dict) -> tuple[bool, str]:
    """Validate uploaded credentials JSON for Gmail Web OAuth.

    Returns (is_valid, error_message).
    Only accepts Web application OAuth credentials.
    """
    if not isinstance(data, dict):
        return False, "Invalid JSON: must be an object"

    # Only accept Web application credentials
    if "web" not in data:
        if "installed" in data:
            return False, "Wrong OAuth credential type. Upload Web application OAuth JSON."
        return False, "Missing 'web' OAuth client info"

    web = data["web"]
    if not isinstance(web, dict):
        return False, "Web client info must be an object"

    # Validate required fields
    required_fields = ["client_id", "client_secret", "auth_uri", "token_uri"]
    for field in required_fields:
        if field not in web:
            return False, f"Missing '{field}' in web credentials"
        if not web[field]:
            return False, f"'{field}' is empty"

    # Validate redirect URIs
    redirect_uris = web.get("redirect_uris", [])
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return False, "Missing 'redirect_uris' array in web credentials"
    
    if EXPECTED_REDIRECT_URI not in redirect_uris:
        return False, f"Redirect URI {EXPECTED_REDIRECT_URI} not found in web credentials"

    # Validate JavaScript origins
    javascript_origins = web.get("javascript_origins", [])
    if not isinstance(javascript_origins, list) or not javascript_origins:
        return False, "Missing 'javascript_origins' array in web credentials"
    
    if "http://127.0.0.1:8000" not in javascript_origins:
        return False, "JavaScript origin http://127.0.0.1:8000 not found in web credentials"

    return True, ""


def save_credentials_json(data: dict) -> bool:
    """Save validated credentials JSON to file.

    Never logs or exposes client_secret.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Strip any sensitive logging before saving
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Gmail OAuth credentials saved to %s", CREDENTIALS_FILE)
        return True
    except Exception as e:
        logger.error("Failed to save credentials: %s", e)
        return False


def _validate_web_credentials_for_auth(creds_data: dict) -> Optional[str]:
    """Validate web credentials have all required fields for OAuth.

    Returns None if valid, or an error message string if invalid.
    Never exposes client_secret in the returned message.
    """
    web = creds_data.get("web")
    if not web or not isinstance(web, dict):
        return "Credentials missing 'web' block for Web application OAuth"

    if not web.get("client_id"):
        return "Missing client_id in OAuth credentials"

    if not web.get("client_secret"):
        return "Missing client_secret in OAuth credentials"

    if not web.get("auth_uri"):
        return "Missing auth_uri in OAuth credentials"

    if not web.get("token_uri"):
        return "Missing token_uri in OAuth credentials"

    redirect_uris = web.get("redirect_uris", [])
    if EXPECTED_REDIRECT_URI not in redirect_uris:
        return (
            f"Redirect URI {EXPECTED_REDIRECT_URI} not found in credentials. "
            f"Configured URIs: {redirect_uris}"
        )

    return None


def get_authorization_url() -> tuple[Optional[str], Optional[str]]:
    """Generate Google OAuth authorization URL using direct OAuth2 web flow.
    
    Always generates a fresh URL with new state to prevent stale authorization codes.
    Returns (url, error_message) tuple.
    On success: (url_string, None)
    On failure: (None, error_message_string)
    Never exposes client_secret in error_message.
    """
    creds_data = _get_credentials()
    if creds_data is None:
        logger.warning("Cannot start OAuth: credentials not found or invalid JSON")
        return (None, "Gmail OAuth credentials file not found or contains invalid JSON")

    # Only accept Web credentials
    if "web" not in creds_data:
        if "installed" in creds_data:
            return (None, "Wrong OAuth credential type. Upload Web application OAuth JSON.")
        return (None, "Invalid credentials format. Must contain 'web' key.")

    web = creds_data["web"]
    
    # Validate credentials
    is_valid, error = validate_credentials_json(creds_data)
    if not is_valid:
        logger.warning("Web credentials validation failed: %s", error)
        return (None, error)

    try:
        # Generate fresh state token
        state = secrets.token_urlsafe(32)
        
        # Save state temporarily
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(OAUTH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"state": state}, f)
        
        # Build authorization URL manually - ensure scope is properly formatted
        base_url = "https://accounts.google.com/o/oauth2/v2/auth"
        scope_string = " ".join(SCOPES_READ_ONLY)
        
        params = {
            "client_id": web["client_id"],
            "redirect_uri": EXPECTED_REDIRECT_URI,
            "response_type": "code",
            "scope": scope_string,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        
        # Use urlencode to properly encode all parameters
        auth_url = f"{base_url}?{urllib.parse.urlencode(params)}"
        
        # Safe debug logging
        logger.info("OAuth authorization URL generated with params: client_id=yes redirect_uri=yes scope_count=%d state=yes", len(SCOPES_READ_ONLY))
        
        return (auth_url, None)

    except Exception as e:
        error_str = str(e)
        logger.error("Failed to generate authorization URL: %s", error_str)
        return (None, f"Failed to generate authorization URL: {error_str[:200]}")


def exchange_code_for_token(code: str, state: str = None) -> tuple[bool, str]:
    """Exchange OAuth authorization code for token using direct OAuth2 web flow.

    Returns (success, message_or_email).
    Never logs or exposes token values, refresh tokens, or client secrets.
    """
    try:
        creds_data = _get_credentials()
        if creds_data is None:
            logger.warning("OAuth token exchange failed: credentials not found")
            return False, "Credentials not found"

        if "web" not in creds_data:
            logger.warning("OAuth token exchange failed: not web credentials")
            return False, "Invalid credentials format"

        web = creds_data["web"]

        # Verify state if provided
        if state is not None:
            try:
                if OAUTH_STATE_FILE.exists():
                    with open(OAUTH_STATE_FILE, "r", encoding="utf-8") as f:
                        state_data = json.load(f)
                    expected_state = state_data.get("state")
                    if state != expected_state:
                        logger.warning("OAuth state mismatch")
                        return False, "State mismatch. Please try connecting again."
                    # Clean up state file after use
                    OAUTH_STATE_FILE.unlink()
                else:
                    logger.warning("OAuth state file missing")
                    return False, "State verification failed. Please try connecting again."
            except Exception as e:
                logger.warning("OAuth state verification failed: %s", e)
                return False, "State verification failed. Please try connecting again."

        # Exchange code for token using direct HTTP request
        token_url = web["token_uri"]
        token_data = {
            "code": code,
            "client_id": web["client_id"],
            "client_secret": web["client_secret"],
            "redirect_uri": EXPECTED_REDIRECT_URI,
            "grant_type": "authorization_code",
        }

        logger.info("Token exchange HTTP request started")
        response = requests.post(token_url, data=token_data, timeout=30)
        
        logger.info("Token exchange HTTP status: %s", response.status_code)
        
        if response.status_code != 200:
            # Read safe error information
            try:
                error_data = response.json()
                error_code = error_data.get("error", "unknown_error")
                error_description = error_data.get("error_description", "Token exchange failed")
                logger.warning("Token exchange failed: %s", error_code)
                
                if error_code == "invalid_grant":
                    return False, "Authorization code expired or already used. Please try connecting again."
                elif error_code == "invalid_client":
                    return False, "Invalid OAuth credentials. Please check your Web application configuration."
                else:
                    return False, f"Token exchange failed: {error_description[:100]}"
            except:
                return False, "Token exchange failed with HTTP error"

        # Parse successful token response
        token_response = response.json()
        
        # Build token data compatible with google-auth Credentials
        token_data = {
            "token": token_response.get("access_token"),  # Map access_token to token
            "refresh_token": token_response.get("refresh_token"),
            "token_uri": web["token_uri"],
            "client_id": web["client_id"],
            "client_secret": web["client_secret"],
            "scopes": SCOPES_READ_ONLY,
        }
        
        # Add expiry if available
        if "expires_in" in token_response:
            # Convert expires_in seconds to ISO format timestamp
            from datetime import datetime, timedelta
            expiry = datetime.utcnow() + timedelta(seconds=int(token_response["expires_in"]))
            token_data["expiry"] = expiry.isoformat() + "Z"

        # Save token
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(token_data, f, indent=2)

        logger.info("Token saved: yes")
        logger.info("Token file exists: yes")
        reset_service()
        mark_token_fresh()

        # Try to get account email
        logger.info("Gmail profile fetch: attempting")
        email = get_account_email()
        if email:
            logger.info("Gmail profile fetch: success")
            return True, email
        logger.info("Gmail profile fetch: failure (no email returned)")
        return True, "Token saved (email unknown)"

    except requests.RequestException as e:
        logger.error("HTTP request failed during token exchange: %s", e)
        return False, "Network error during token exchange. Please try again."
    except Exception as e:
        error_str = str(e)
        logger.error("Failed to exchange code for token: %s", e)
        return False, f"Token exchange failed: {error_str[:200]}"


# Keep the old function for backward compatibility but make it use the new implementation  
def exchange_code_for_token_with_response(authorization_response: str) -> tuple[bool, str]:
    """Exchange OAuth authorization response for token.
    
    Parses the authorization response URL and extracts code/state for token exchange.
    Returns (success, message_or_email).
    Never logs or exposes token values, refresh tokens, or client secrets.
    """
    try:
        # Parse the authorization response URL
        parsed_url = urllib.parse.urlparse(authorization_response)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        
        # Extract code and state
        code = query_params.get("code", [None])[0]
        state = query_params.get("state", [None])[0]
        error = query_params.get("error", [None])[0]
        
        if error:
            logger.warning("Authorization response contains error: %s", error)
            return False, "Access denied by user"
            
        if not code:
            logger.warning("Authorization response missing code")
            return False, "Missing authorization code"
            
        # Use the direct code exchange
        return exchange_code_for_token(code, state)
        
    except Exception as e:
        logger.error("Failed to parse authorization response: %s", e)
        return False, "Invalid authorization response"


def get_account_email() -> Optional[str]:
    """Get the authenticated Gmail account email address.

    Returns email string, or None if not available.
    """
    service = _build_service()
    if service is None:
        return None

    try:
        profile = service.users().getProfile(userId="me").execute()
        return profile.get("emailAddress", None)
    except Exception as e:
        logger.warning("Failed to get account email: %s", e)
        return None


def delete_token() -> bool:
    """Delete the Gmail token file.

    Returns True if deleted or not found, False on error.
    """
    global _gmail_service, _credentials_available
    _gmail_service = None
    _credentials_available = False

    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
            logger.info("Gmail token deleted")
        return True
    except Exception as e:
        logger.error("Failed to delete token: %s", e)
        return False


def delete_credentials() -> bool:
    """Delete the Gmail credentials file.

    Returns True if deleted or not found, False on error.
    """
    global _gmail_service, _credentials_available
    _gmail_service = None
    _credentials_available = False

    try:
        if CREDENTIALS_FILE.exists():
            CREDENTIALS_FILE.unlink()
            logger.info("Gmail credentials deleted")
        return True
    except Exception as e:
        logger.error("Failed to delete credentials: %s", e)
        return False


# ── Existing Gmail API Methods ────────────────────────────────────────────


def list_recent_messages(max_results: int = 20) -> list[GmailMessagePreview]:
    """Fetch recent messages from Gmail inbox.

    Returns a list of GmailMessagePreview objects (privacy-minimized).
    Returns empty list if not connected or on error.
    """
    service = _build_service()
    if service is None:
        logger.warning("Gmail service not available")
        return []

    try:
        # Get list of messages from inbox
        results = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q="in:inbox")
            .execute()
        )

        messages = results.get("messages", [])
        if not messages:
            return []

        previews = []
        for msg in messages:
            try:
                preview = _get_message_preview(service, msg["id"])
                if preview:
                    previews.append(preview)
            except Exception as e:
                logger.warning("Failed to process message %s: %s", msg["id"], e)
                continue

        return previews

    except Exception as e:
        logger.error("Failed to list Gmail messages: %s", e)
        return []


def list_recent_unread(max_results: int = 20) -> list[GmailMessagePreview]:
    """Fetch recent UNREAD messages from Gmail inbox.

    Retries once on SSL/network/read timeout errors by resetting the
    service and building a fresh transport.
    """
    service = _build_service()
    if service is None:
        return []

    try:
        results = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q="in:inbox is:unread")
            .execute()
        )
    except Exception as e:
        err_lower = str(e).lower()
        is_timeout = any(w in err_lower for w in ("read operation timed out", "timed out", "timeout"))
        is_ssl = any(w in err_lower for w in ("ssl", "connection", "wrong_version"))
        if is_timeout or is_ssl:
            if is_timeout:
                logger.warning("Gmail list unread timed out once; retrying with fresh service")
            else:
                logger.warning("Gmail list unread failed once; retrying with fresh service")
            reset_service()
            service = _build_service()
            if service is None:
                logger.warning("Gmail list unread retry aborted: service still unavailable")
                return []
            try:
                results = (
                    service.users()
                    .messages()
                    .list(userId="me", maxResults=max_results, q="in:inbox is:unread")
                    .execute()
                )
            except Exception as retry_err:
                logger.warning("Gmail list unread retry also failed: %s", retry_err)
                return []
        else:
            logger.error("Failed to list unread Gmail messages: %s", e)
            return []

    messages = results.get("messages", [])
    if not messages:
        return []

    previews = []
    for msg in messages:
        try:
            preview = _get_message_preview(service, msg["id"])
            if preview:
                previews.append(preview)
        except Exception as e:
            err_lower = str(e).lower()
            if "ssl" in err_lower or "connection" in err_lower or "wrong_version" in err_lower:
                logger.warning("Skipped Gmail message preview due to connection error. Will retry later.")
            else:
                logger.warning("Failed to process message %s (safe reason)", msg["id"])
            continue

    return previews


def list_messages_for_period(period: str = "today", max_results: int = 500) -> list[GmailMessagePreview]:
    """Fetch inbox messages in the selected period, including read messages.

    This is deliberately separate from the unread-only polling helper: the
    initial selected-period scan must be able to review the whole inbox.
    """
    service = _build_service()
    if service is None:
        return []

    days_back = {"today": 1, "week": 7, "month": 30}.get(period, 1)
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y/%m/%d")
    query = f"in:inbox after:{start}"
    messages: list[dict] = []
    page_token: Optional[str] = None
    try:
        while len(messages) < max_results:
            response = service.users().messages().list(
                userId="me", q=query, maxResults=min(100, max_results - len(messages)),
                pageToken=page_token,
            ).execute()
            messages.extend(response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        logger.error("Failed to list Gmail messages for selected period: %s", exc)
        return []

    previews = []
    for message in messages:
        preview = _get_message_preview(service, message.get("id", ""))
        if preview:
            previews.append(preview)
    return previews


def _get_message_preview(service: Any, message_id: str) -> Optional[GmailMessagePreview]:
    """Get a privacy-minimized preview of a single message."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata")
            .execute()
        )

        headers = _parse_headers(msg.get("payload", {}).get("headers", []))
        sender = headers.get("From", "")
        subject = headers.get("Subject", "")
        date = headers.get("Date", "")
        snippet = msg.get("snippet", "")[:200]

        # Get attachment filenames
        attachment_names = _get_attachment_filenames(msg)

        # Get a short body preview (privacy-minimized)
        body_preview = _get_body_preview(msg, max_chars=500)

        thread_id = msg.get("threadId", "")
        return GmailMessagePreview(
            message_id=message_id,
            thread_id=thread_id,
            sender=sender,
            subject=subject,
            snippet=snippet,
            date=date,
            attachment_filenames=attachment_names,
            body_preview=body_preview,
        )

    except Exception as e:
        logger.warning("Error getting message preview for %s: %s", message_id, e)
        return None


def _parse_headers(headers: list[dict]) -> dict[str, str]:
    """Parse email headers into a dict."""
    result = {}
    for h in headers:
        name = h.get("name", "")
        value = h.get("value", "")
        if name:
            result[name] = value
    return result


def _get_attachment_filenames(msg: dict) -> list[str]:
    """Extract attachment filenames from a message."""
    filenames = []
    payload = msg.get("payload", {})

    # Check main payload
    if payload.get("filename"):
        filenames.append(payload["filename"])

    # Check parts
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("filename"):
            filenames.append(part["filename"])
        # Nested parts
        for subpart in part.get("parts", []):
            if subpart.get("filename"):
                filenames.append(subpart["filename"])

    return filenames


def _get_body_preview(msg: dict, max_chars: int = 500) -> str:
    """Get a short body preview from a message (privacy-minimized)."""
    try:
        payload = msg.get("payload", {})
        parts = payload.get("parts", [])

        # Try to get plain text body
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    # Remove excessive whitespace
                    decoded = re.sub(r"\s+", " ", decoded).strip()
                    return decoded[:max_chars]

        # Fall back to snippet
        return msg.get("snippet", "")[:max_chars]

    except Exception:
        return msg.get("snippet", "")[:max_chars]


def download_attachment(
    message_id: str, attachment_id: str, filename: str, save_dir: Path
) -> Optional[Path]:
    """Download a single attachment from a Gmail message.

    Args:
        message_id: Gmail message ID
        attachment_id: Attachment ID within the message
        filename: Original filename
        save_dir: Directory to save to

    Returns:
        Path to saved file, or None on failure.
    """
    service = _build_service()
    if service is None:
        return None

    try:
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )

        data = attachment.get("data", "")
        if not data:
            logger.warning("No data in attachment %s", attachment_id)
            return None

        file_data = base64.urlsafe_b64decode(data)

        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / _safe_filename(filename)

        with open(save_path, "wb") as f:
            f.write(file_data)

        logger.info("Saved attachment %s to %s", filename, save_path)
        return save_path

    except Exception as e:
        logger.error("Failed to download attachment %s: %s", attachment_id, e)
        return None


def _safe_filename(filename: str) -> str:
    """Sanitize a filename to prevent path traversal."""
    # Remove path separators
    filename = filename.replace("\\", "_").replace("/", "_")
    # Remove null bytes
    filename = filename.replace("\x00", "")
    # Limit length
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:190] + ext
    return filename


def get_attachment_ids_and_names(
    message_id: str,
) -> list[dict]:
    """Get attachment IDs and filenames for a message.

    Returns list of dicts with 'id', 'filename', 'mimeType'.
    """
    service = _build_service()
    if service is None:
        return []

    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

        attachments = []
        payload = msg.get("payload", {})
        parts = payload.get("parts", [])

        for part in parts:
            if part.get("filename"):
                body = part.get("body", {})
                att_id = body.get("attachmentId")
                if att_id:
                    attachments.append(
                        {
                            "id": att_id,
                            "filename": part["filename"],
                            "mimeType": part.get("mimeType", ""),
                        }
                    )

        return attachments

    except Exception as e:
        logger.warning("Failed to get attachment IDs for %s: %s", message_id, e)
        return []


def create_draft(to: str, subject: str, body: str) -> Optional[str]:
    """Create a Gmail draft reply.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body text

    Returns:
        Draft ID if successful, None on failure.
    """
    service = _build_service()
    if service is None:
        logger.warning("Gmail service not available, cannot create draft")
        return None

    try:
        import email.mime.text

        # The From header is intentionally omitted here.
        # Fetching the account email makes an extra API call that can fail
        # with SSL errors in background threads.
        # Gmail auto-populates From from the authenticated account.
        message = email.mime.text.MIMEText(body)
        message["To"] = to
        message["Subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )

        draft_id = draft.get("id", "")
        if not draft_id:
            logger.warning("Gmail draft creation returned empty id")
            return None

        logger.info("Gmail draft created (id prefix): %s", draft_id[:8])
        return draft_id

    except Exception as e:
        logger.error("Failed to create draft: %s", e)
        return None


def verify_draft_exists(draft_id: str) -> bool:
    """Verify that a Gmail draft actually exists using drafts.get.

    Returns True only if the draft can be fetched from Gmail API.
    Never logs or exposes draft content.
    """
    service = _build_service()
    if service is None:
        return False
    if not draft_id:
        return False
    try:
        result = service.users().drafts().get(
            userId="me", id=draft_id, format="metadata"
        ).execute()
        exists = bool(result.get("id"))
        logger.info("Draft verification: id=%s exists=%s", draft_id[:8], exists)
        return exists
    except Exception as e:
        # 404 means draft does not exist
        logger.warning("Draft verification failed (id=%s): %s", draft_id[:8], str(e)[:60])
        return False


def list_gmail_drafts(max_results: int = 50) -> list[dict]:
    """Fetch list of Gmail drafts from the API.

    Returns safe metadata only — no message bodies, no tokens.
    """
    service = _build_service()
    if service is None:
        return []
    try:
        result = service.users().drafts().list(
            userId="me", maxResults=max_results
        ).execute()
        drafts_raw = result.get("drafts", [])
        safe_drafts = []
        for d in drafts_raw:
            draft_id = d.get("id", "")
            if not draft_id:
                continue
            # Fetch metadata for subject/to (no body)
            try:
                detail = service.users().drafts().get(
                    userId="me", id=draft_id, format="metadata"
                ).execute()
                msg = detail.get("message", {})
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                    if h.get("name") and h.get("value")
                }
                safe_drafts.append({
                    "id": draft_id[:16],  # Truncated — not a secret but keeps it short
                    "subject": headers.get("subject", "")[:200],
                    "to": headers.get("to", "")[:100],
                    "date": headers.get("date", "")[:50],
                    "internal_date": msg.get("internalDate", ""),
                })
            except Exception:
                # If we can't fetch details, still include the ID
                safe_drafts.append({"id": draft_id[:16], "subject": "", "to": "", "date": ""})
        return safe_drafts
    except Exception as e:
        logger.warning("Failed to list Gmail drafts: %s", str(e)[:80])
        return []


def get_draft_scope_available() -> bool:
    """Check if Gmail draft scope is available by attempting a safe drafts.list call.

    Returns True if scope is available, False if permission denied.
    """
    service = _build_service()
    if service is None:
        return False
    try:
        service.users().drafts().list(userId="me", maxResults=1).execute()
        return True
    except Exception as e:
        err = str(e).lower()
        if any(w in err for w in ("insufficient", "scope", "permission", "403", "forbidden")):
            return False
        return True  # Other errors don't mean scope is missing


def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email via Gmail API.

    Only call this when auto_send_enabled is explicitly True and all
    safety checks have passed.

    Args:
        to: Recipient email address
        subject: Email subject
        body: Email body text

    Returns:
        True if sent successfully, False otherwise.
    """
    service = _build_service()
    if service is None:
        logger.warning("Gmail service not available, cannot send email")
        return False

    try:
        import email.mime.text

        message = email.mime.text.MIMEText(body)
        message["To"] = to
        message["Subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )

        logger.info("Sent email to %s: message ID %s", to, sent.get("id", ""))
        return True

    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False


def mark_as_read(message_id: str) -> bool:
    """Mark a message as read."""
    service = _build_service()
    if service is None:
        return False

    try:
        service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return True
    except Exception as e:
        logger.warning("Failed to mark message %s as read: %s", message_id, e)
        return False
