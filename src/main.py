import argparse
import asyncio
import sys

from browser import launch_browser, login_session, check_login_status, set_debug
from twitter import (
    list_followers,
    list_following,
    follow_user,
    unfollow_user,
    username_from_url,
)
from churn import run_churn, run_reconcile


async def _with_browser(headless: bool, fn):
    pw, context, page = await launch_browser(headless=headless)
    try:
        await page.goto("https://x.com/home", wait_until="domcontentloaded")
        if not await check_login_status(page):
            print("Not logged in. Run: python src/main.py login", file=sys.stderr)
            return 2
        return await fn(page)
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()


async def cmd_followers(args):
    user = username_from_url(args.username)

    async def run(page):
        rows = await list_followers(page, user, max_users=args.max)
        for r in rows:
            if r["is_private"]:
                continue
            print(r["profile_url"])
        print(f"# {len([r for r in rows if not r['is_private']])} public ({len(rows)} total)", file=sys.stderr)
        return 0

    return await _with_browser(args.headful is False, run)


async def cmd_following(args):
    user = username_from_url(args.username)

    async def run(page):
        rows = await list_following(page, user, max_users=args.max)
        for r in rows:
            if r["is_private"]:
                continue
            print(r["profile_url"])
        print(f"# {len([r for r in rows if not r['is_private']])} public ({len(rows)} total)", file=sys.stderr)
        return 0

    return await _with_browser(args.headful is False, run)


async def cmd_follow(args):
    async def run(page):
        result = await follow_user(page, args.profile_url, skip_private=not args.include_private)
        print(result)
        return 0 if result["ok"] else 1

    return await _with_browser(args.headful is False, run)


async def cmd_unfollow(args):
    async def run(page):
        result = await unfollow_user(page, args.profile_url)
        print(result)
        return 0 if result["ok"] else 1

    return await _with_browser(args.headful is False, run)


async def _scrape_self_username(page) -> str | None:
    """Read the logged-in handle from X's side-nav profile link."""
    try:
        href = await page.locator('a[data-testid="AppTabBar_Profile_Link"]').first.get_attribute(
            "href", timeout=10000
        )
        if href and href.startswith("/"):
            return href.lstrip("/").split("/")[0]
    except Exception:
        pass
    return None


async def cmd_whoami(args):
    from identity import set_username, PROFILE_FILE
    if args.username:
        set_username(args.username)
        print(f"wrote {PROFILE_FILE}: {args.username}")
        return 0

    async def run(page):
        u = await _scrape_self_username(page)
        if not u:
            print("ERROR: could not scrape the logged-in handle. "
                  "Rerun with --username <handle>.", file=sys.stderr)
            return 1
        set_username(u)
        print(f"wrote {PROFILE_FILE}: {u}")
        return 0

    return await _with_browser(args.headful is False, run)


def build_parser():
    p = argparse.ArgumentParser(prog="twitterbot3")
    p.add_argument("--debug", action="store_true", help="dump HTML + screenshots to data/debug/")
    p.add_argument("--headful", action="store_true", help="show the browser window")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("login", help="one-time interactive login")

    sp = sub.add_parser("followers", help="list followers of a user")
    sp.add_argument("username")
    sp.add_argument("--max", type=int, default=None)

    sp = sub.add_parser("following", help="list who a user follows")
    sp.add_argument("username")
    sp.add_argument("--max", type=int, default=None)

    sp = sub.add_parser("follow", help="follow a user by profile URL")
    sp.add_argument("profile_url")
    sp.add_argument("--include-private", action="store_true",
                    help="also send follow request to private accounts")

    sp = sub.add_parser("unfollow", help="unfollow a user by profile URL")
    sp.add_argument("profile_url")

    sp = sub.add_parser("churn", help="run the full churn flow (unfollow stale + follow new)")
    sp.add_argument("--dry-run", action="store_true",
                    help="don't follow/unfollow anything — just print what would happen")

    sp = sub.add_parser("reconcile",
                        help="re-check every status=error follow entry and rewrite the log to match actual state")

    sp = sub.add_parser("whoami",
                        help="capture the logged-in handle into data/profile.json (scrapes the authed browser)")
    sp.add_argument("--username", help="override -- write directly without a browser scrape")

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    set_debug(bool(args.debug))

    if args.command == "login":
        asyncio.run(login_session())
        print("Logged in. Next step: python src/main.py whoami "
              "(captures your handle into data/profile.json).")
        return 0

    if args.command == "whoami":
        return asyncio.run(cmd_whoami(args)) or 0

    if args.command == "churn":
        return asyncio.run(run_churn(dry_run=bool(args.dry_run), headful=bool(args.headful))) or 0

    if args.command == "reconcile":
        return asyncio.run(run_reconcile(headful=bool(args.headful))) or 0

    handlers = {
        "followers": cmd_followers,
        "following": cmd_following,
        "follow": cmd_follow,
        "unfollow": cmd_unfollow,
    }
    fn = handlers[args.command]
    return asyncio.run(fn(args)) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as _e:
        # Startup / unhandled crash above the churn try/except. Best-effort
        # upload so the dashboard sees the failure even if the bot exits hard.
        import traceback as _tb
        try:
            from supabase_client import upload_error
            from identity import get_username
            upload_error({
                "account":   get_username(),
                "source":    "main",
                "kind":      "startup",
                "exit_code": 1,
                "message":   f"{type(_e).__name__}: {_e}",
                "traceback": _tb.format_exc(),
            })
        except Exception:
            pass
        raise
