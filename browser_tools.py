from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from jarvis_core.config import WEB_SCREENSHOT_DIR


JARVIS_DIR = WEB_SCREENSHOT_DIR.parent  # repository root / JARVIS_BASE_DIR
WEB_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def normalize_url(url: str) -> str:
    url = url.strip()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only HTTP and HTTPS websites are allowed.")

    if not parsed.netloc:
        raise ValueError("Please provide a valid website address.")

    return url


def read_webpage(url: str) -> dict:
    safe_url = normalize_url(url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            viewport={
                "width": 1366,
                "height": 768
            }
        )

        try:
            page.goto(
                safe_url,
                wait_until="domcontentloaded",
                timeout=60000
            )

            title = page.title()

            try:
                text = page.locator("body").inner_text(
                    timeout=15000
                )
            except Exception:
                text = "The webpage opened, but readable body text was unavailable."

            return {
                "status": "done",
                "url": page.url,
                "title": title,
                "content": text[:7000]
            }

        finally:
            browser.close()


def screenshot_webpage(url: str) -> dict:
    safe_url = normalize_url(url)

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    file_path = (
        WEB_SCREENSHOT_DIR
        / f"website_{timestamp}.png"
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            viewport={
                "width": 1366,
                "height": 768
            }
        )

        try:
            page.goto(
                safe_url,
                wait_until="domcontentloaded",
                timeout=60000
            )

            page.screenshot(
                path=str(file_path),
                full_page=True
            )

            return {
                "status": "done",
                "message": "Website screenshot captured.",
                "url": page.url,
                "title": page.title(),
                "file": str(file_path)
            }

        finally:
            browser.close()