"""Smoke test: follow a single user by handle or URL.

Usage:
    poetry run python tests/follow-user.py <username-or-url>

Exit code is 0 on a successful follow (or no-op if already following),
1 on any error, 2 if not logged in.
"""

import argparse
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from browser import launch_browser, check_login_status  # noqa: E402
from twitter import follow_user, username_from_url  # noqa: E402


async def main_async(target: str, headful: bool) -> int:
    user = username_from_url(target)
    url = f"https://x.com/{user}"
    print(f"[test] follow target: {url}")

    pw, context, page = await launch_browser(headless=not headful)
    try:
        await page.goto("https://x.com/home", wait_until="domcontentloaded")
        if not await check_login_status(page):
            print("[test] not logged in — run: python src/main.py login", file=sys.stderr)
            return 2
        result = await follow_user(page, url, skip_private=False)
        print(f"[test] result: {result}")
        return 0 if result.get("ok") else 1
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("target", help="username or full profile URL")
    p.add_argument("--headful", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args.target, args.headful)))


if __name__ == "__main__":
    main()
