"""X/Twitter actions: list followers/following, follow, unfollow.

Selectors are based on x.com DOM as of 2026-05. Key anchors:
  - User row in a list:   [data-testid="UserCell"]
  - Username on row:      [data-testid^="UserAvatar-Container-"]  (suffix is the screen_name)
  - Profile link on row:  a[role="link"][href="/<username>"]
  - Private (locked):     descendant [data-testid="icon-lock"] inside the UserCell
  - Profile-page follow:  [data-testid$="-follow"]   (aria-label "Follow @x" or "Follow back @x")
  - Profile-page unfollow:[data-testid$="-unfollow"] (aria-label "Following @x")
  - Pending (private req):[data-testid$="-cancel"]   (label text "Pending")
"""

import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse

from browser import human_delay, dump_page, ACTIONS_LOG, DATA_DIR


def _log_action(action: str, profile_url: str, result: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action,
        "profile_url": profile_url,
        "username": username_from_url(profile_url),
        "ok": result.get("ok"),
        "status": result.get("status"),
        "reason": result.get("reason", ""),
    }
    with open(ACTIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

X_BASE = "https://x.com"


def username_from_url(url_or_handle: str) -> str:
    s = url_or_handle.strip()
    if s.startswith("@"):
        return s[1:]
    if "://" in s:
        path = urlparse(s).path.strip("/")
        return path.split("/")[0]
    return s.strip("/").split("/")[0]


async def _wait_for_list(page, timeout_ms: int = 15000) -> bool:
    try:
        await page.wait_for_selector('[data-testid="UserCell"], [data-testid="primaryColumn"]', timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _collect_visible(page) -> list[dict]:
    """Pull (username, is_private) for every UserCell currently in the DOM."""
    rows = await page.eval_on_selector_all(
        '[data-testid="UserCell"]',
        """
        cells => cells.map(cell => {
            let username = null;
            const av = cell.querySelector('[data-testid^="UserAvatar-Container-"]');
            if (av) {
                username = av.getAttribute('data-testid').replace('UserAvatar-Container-', '');
            }
            if (!username) {
                const a = cell.querySelector('a[role="link"][href^="/"]');
                if (a) {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/^\\/([^\\/?#]+)/);
                    if (m) username = m[1];
                }
            }
            const isPrivate = !!cell.querySelector('[data-testid="icon-lock"]');
            return { username, isPrivate };
        })
        """,
    )
    out = []
    for r in rows:
        u = (r.get("username") or "").strip()
        if not u or u.lower() in {"i", "home", "explore", "notifications", "messages", "search"}:
            continue
        out.append({"username": u, "is_private": bool(r.get("isPrivate"))})
    return out


async def _scroll_and_collect(page, max_users: int | None = None) -> list[dict]:
    seen: dict[str, dict] = {}
    stagnant_rounds = 0
    last_count = 0

    while True:
        for r in await _collect_visible(page):
            if r["username"] not in seen:
                seen[r["username"]] = r
        if max_users is not None and len(seen) >= max_users:
            break
        if len(seen) == last_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
            last_count = len(seen)
        if stagnant_rounds >= 4:
            break
        await page.mouse.wheel(0, 2500)
        await human_delay(0.8, 1.6)

    items = list(seen.values())
    if max_users is not None:
        items = items[:max_users]
    return items


async def list_followers(page, username: str, max_users: int | None = None) -> list[dict]:
    """Visit /{username}/followers and scrape rows. Returns [{username, profile_url, is_private}]."""
    url = f"{X_BASE}/{username}/followers"
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(2.0, 3.5)
    if not await _wait_for_list(page):
        await dump_page(page, f"followers-{username}-noload", force=True)
        return []
    rows = await _scroll_and_collect(page, max_users=max_users)
    return [
        {
            "username": r["username"],
            "profile_url": f"{X_BASE}/{r['username']}",
            "is_private": r["is_private"],
        }
        for r in rows
    ]


async def list_following(page, username: str, max_users: int | None = None) -> list[dict]:
    url = f"{X_BASE}/{username}/following"
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(2.0, 3.5)
    if not await _wait_for_list(page):
        await dump_page(page, f"following-{username}-noload", force=True)
        return []
    rows = await _scroll_and_collect(page, max_users=max_users)
    return [
        {
            "username": r["username"],
            "profile_url": f"{X_BASE}/{r['username']}",
            "is_private": r["is_private"],
        }
        for r in rows
    ]


async def _is_protected_profile(page) -> bool:
    try:
        loc = page.locator('[data-testid="icon-lock"]')
        return await loc.count() > 0
    except Exception:
        return False


async def _find_profile_action_button(page):
    """Return the primary follow/unfollow/pending button on a profile page, or None."""
    selectors = [
        '[data-testid$="-follow"]',
        '[data-testid$="-unfollow"]',
        '[data-testid$="-cancel"]',
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            return loc
    return None


async def _button_state(button) -> str:
    """Returns 'follow', 'unfollow', 'pending', or 'unknown'."""
    try:
        tid = await button.get_attribute("data-testid") or ""
        if tid.endswith("-follow"):
            return "follow"
        if tid.endswith("-unfollow"):
            return "unfollow"
        if tid.endswith("-cancel"):
            return "pending"
    except Exception:
        pass
    return "unknown"


async def follow_user(page, profile_url: str, skip_private: bool = True) -> dict:
    """Navigate to a profile and click Follow. Returns {ok, status, reason}."""
    result = await _follow_user_impl(page, profile_url, skip_private)
    _log_action("follow", profile_url, result)
    return result


async def _follow_user_impl(page, profile_url: str, skip_private: bool) -> dict:
    await page.goto(profile_url, wait_until="domcontentloaded")
    await human_delay(1.5, 3.0)

    if skip_private and await _is_protected_profile(page):
        return {"ok": False, "status": "skipped", "reason": "private"}

    btn = await _find_profile_action_button(page)
    if not btn:
        await dump_page(page, "follow-no-button", force=True)
        return {"ok": False, "status": "error", "reason": "follow button not found"}

    state = await _button_state(btn)
    if state == "unfollow":
        return {"ok": True, "status": "noop", "reason": "already following"}
    if state == "pending":
        return {"ok": True, "status": "noop", "reason": "follow request already pending"}
    if state != "follow":
        return {"ok": False, "status": "error", "reason": f"unexpected button state: {state}"}

    try:
        await btn.scroll_into_view_if_needed()
        await human_delay(0.3, 0.9)
        await btn.click()
        await human_delay(1.2, 2.2)
    except Exception as e:
        return {"ok": False, "status": "error", "reason": f"click failed: {e}"}

    # Re-check button to confirm transition.
    after = await _find_profile_action_button(page)
    after_state = await _button_state(after) if after else "unknown"
    if after_state in ("unfollow", "pending"):
        return {"ok": True, "status": "followed", "reason": after_state}
    return {"ok": False, "status": "error", "reason": f"state after click: {after_state}"}


async def unfollow_user(page, profile_url: str) -> dict:
    result = await _unfollow_user_impl(page, profile_url)
    _log_action("unfollow", profile_url, result)
    return result


async def _unfollow_user_impl(page, profile_url: str) -> dict:
    await page.goto(profile_url, wait_until="domcontentloaded")
    await human_delay(1.5, 3.0)

    btn = await _find_profile_action_button(page)
    if not btn:
        await dump_page(page, "unfollow-no-button", force=True)
        return {"ok": False, "status": "error", "reason": "action button not found"}

    state = await _button_state(btn)
    if state == "follow":
        return {"ok": True, "status": "noop", "reason": "not following"}
    if state not in ("unfollow", "pending"):
        return {"ok": False, "status": "error", "reason": f"unexpected button state: {state}"}

    try:
        await btn.scroll_into_view_if_needed()
        await human_delay(0.3, 0.9)
        await btn.click()
        await human_delay(0.6, 1.2)
    except Exception as e:
        return {"ok": False, "status": "error", "reason": f"click failed: {e}"}

    # X shows a confirmation dialog for Unfollow. Click the "Unfollow" confirm button.
    try:
        confirm = page.locator('[data-testid="confirmationSheetConfirm"]').first
        if await confirm.count() > 0:
            await human_delay(0.3, 0.8)
            await confirm.click()
            await human_delay(1.0, 2.0)
    except Exception:
        pass

    after = await _find_profile_action_button(page)
    after_state = await _button_state(after) if after else "unknown"
    if after_state == "follow":
        return {"ok": True, "status": "unfollowed", "reason": ""}
    return {"ok": False, "status": "error", "reason": f"state after click: {after_state}"}
