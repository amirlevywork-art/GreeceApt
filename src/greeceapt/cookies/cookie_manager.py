import asyncio
import json
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Playwright, async_playwright

DEFAULT_START_URL = "https://www.xe.gr/en/property/results"


def load_cookies(cookies_path: Path) -> list[dict[str, Any]]:
    """Load cookies from disk (supports list or {'cookies': [...]} formats)."""
    if not cookies_path.exists():
        raise FileNotFoundError(f"cookies.json not found at: {cookies_path}")

    with cookies_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        cookies = data
    elif isinstance(data, dict) and isinstance(data.get("cookies"), list):
        cookies = data["cookies"]
    else:
        raise ValueError("cookies.json format not recognized (expected list or {'cookies': [...]}).")

    now = time.time()
    expired = [c for c in cookies if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0 and c["expires"] < now]
    if expired:
        print(f"[WARN] {len(expired)} of {len(cookies)} cookies are expired. Consider re-running cookie capture.")

    return cookies


def save_cookies(cookies_path: Path, cookies: list[dict[str, Any]]) -> None:
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    with cookies_path.open("w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


async def capture_cookies_interactive(
    playwright: Playwright,
    cookies_path: Path,
    start_url: str = DEFAULT_START_URL,
) -> list[dict[str, Any]]:
    """
    Open a browser window, let the user pass verification/login,
    then persist context cookies to cookies_path.
    """
    browser = await playwright.chromium.launch(headless=False)
    try:
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(start_url, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[WARN] Initial navigation failed ({e}). Continue manually in opened browser.")

        print("\n[COOKIES] Browser opened for cookie capture.")
        print("[COOKIES] Complete login/verification steps in the browser.")
        await asyncio.to_thread(input, "[COOKIES] Press Enter here when ready to save cookies: ")

        cookies = await context.cookies()
        if not cookies:
            raise RuntimeError("No cookies captured from browser context.")

        save_cookies(cookies_path, cookies)
        print(f"[COOKIES] Saved {len(cookies)} cookies -> {cookies_path}")
        return cookies
    finally:
        await browser.close()


async def ensure_cookies(
    playwright: Playwright,
    cookies_path: Path,
    start_url: str = DEFAULT_START_URL,
    auto_capture: bool = True,
) -> list[dict[str, Any]]:
    """
    Return valid cookies from disk. If missing/invalid and auto_capture=True,
    launch browser flow to capture and persist them automatically.
    """
    try:
        return load_cookies(cookies_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        if not auto_capture:
            raise
        print(f"[COOKIES] Existing cookies unavailable ({e}). Starting auto-capture flow.")
        return await capture_cookies_interactive(
            playwright=playwright,
            cookies_path=cookies_path,
            start_url=start_url,
        )


async def run_cli() -> None:
    project_root = Path(__file__).resolve().parents[3]
    cookies_path = project_root / "data" / "cookies.json"

    async with async_playwright() as p:
        await ensure_cookies(
            playwright=p,
            cookies_path=cookies_path,
            start_url=DEFAULT_START_URL,
            auto_capture=True,
        )


if __name__ == "__main__":
    asyncio.run(run_cli())
