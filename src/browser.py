import asyncio
import os
import random
import time

from playwright.async_api import async_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROFILE_DIR = os.path.join(ROOT, "data", "browser_profile")
DEBUG_DIR = os.path.join(ROOT, "data", "debug")
DATA_DIR = os.path.join(ROOT, "data")
ACTIONS_LOG = os.path.join(DATA_DIR, "actions.log")

DEBUG_MODE = False


def set_debug(enabled: bool):
    global DEBUG_MODE
    DEBUG_MODE = enabled


async def launch_browser(headless: bool = True):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    os.makedirs(DEBUG_DIR, exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return pw, context, page


async def login_session():
    pw, context, page = await launch_browser(headless=False)
    await page.goto("https://x.com/login", wait_until="domcontentloaded")
    print("Log in to X. Close the browser window when you're done.")
    try:
        await context.pages[0].wait_for_event("close", timeout=0)
    except Exception:
        pass
    try:
        await context.close()
    except Exception:
        pass
    await pw.stop()


async def check_login_status(page) -> bool:
    url = page.url.lower()
    if "/login" in url or "/i/flow/login" in url or "/account/access" in url:
        return False
    return True


async def human_delay(min_s: float = 1.0, max_s: float = 3.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def dump_page(page, label: str, force: bool = False):
    if not (force or DEBUG_MODE):
        return None
    os.makedirs(DEBUG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(DEBUG_DIR, f"{ts}-{label}")
    try:
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(f"<!-- url: {page.url} -->\n")
            f.write(await page.content())
        await page.screenshot(path=base + ".png", full_page=True)
    except Exception as e:
        print(f"[debug] dump failed: {e}")
    return base
