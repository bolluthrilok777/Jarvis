"""Web / documentation research for the Feature Orchestrator.

Follows the reliability ladder from the spec: structured API/text/DOM data is
preferred; browser automation / OCR are not used in V1. Research output is
always treated as DATA (sanitized), never as Jarvis instructions, and can
never directly trigger Cline.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from jarvis_core.feature_utils import sanitize_web_content

logger = logging.getLogger("jarvis.feature.webresearch")

_REQUEST_TIMEOUT = 8
_MAX_RESULTS = 3

# Known public documentation pointers for common feature domains. Used only to
# choose a *candidate URL*; fetching is best-effort and never blocks a feature.
_DOC_POINTERS = {
    "calendar": ["https://developers.google.com/calendar/api/guides/overview"],
    "gmail": ["https://developers.google.com/gmail/api/guides"],
    "naukri": ["https://www.naukri.com/"],
    "memory": ["https://docs.ollama.com/"],
    "browser automation": ["https://playwright.dev/python/docs/intro"],
    "voice": ["https://github.com/openai/whisper"],
    "telegram": ["https://core.telegram.org/bots/api"],
}

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _extract_text(html: str) -> str:
    """Very lightweight text extraction — no OCR, no heavy parsing."""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_url(url: str) -> dict:
    """Fetch and sanitize a single public URL. Returns a research fact dict."""
    try:
        response = requests.get(url, timeout=_REQUEST_TIMEOUT, headers={
            "User-Agent": "Jarvis-Feature-Research/1.0",
        })
        response.raise_for_status()
    except Exception as exc:
        logger.info("Web research fetch failed for %s: %s", url, exc)
        return {"url": url, "ok": False, "error": type(exc).__name__, "snippet": ""}

    snippet = sanitize_web_content(_extract_text(response.text))[:4000]
    return {"url": url, "ok": True, "snippet": snippet}


def research(objective: str) -> dict:
    """Best-effort web research for a feature objective.

    Returns:
        {
          "sources": [ {url, ok, snippet, error?}, ... ],
          "incomplete": bool,
          "error": Optional[str],
        }
    """
    if not objective:
        return {"sources": [], "incomplete": False, "error": None}

    candidates: list[str] = []
    for url in _URL_RE.findall(objective):
        candidates.append(url)
    lower = objective.lower()
    for domain, urls in _DOC_POINTERS.items():
        if domain in lower:
            candidates.extend(urls)

    sources: list[dict] = []
    failures = 0
    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        facts = fetch_url(url)
        sources.append(facts)
        if not facts.get("ok"):
            failures += 1
        if len(sources) >= _MAX_RESULTS:
            break

    # Research is "incomplete" if we had candidate URLs but all failed.
    incomplete = bool(candidates) and failures == len(sources) and not any(
        s.get("ok") for s in sources
    )
    error = None
    if incomplete and sources:
        error = "; ".join(str(s.get("error")) for s in sources if not s.get("ok"))[:300]
    return {"sources": sources, "incomplete": incomplete, "error": error}