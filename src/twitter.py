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


class RateLimitedError(Exception):
    """Raised when X shows the 'Sorry, you are rate limited' toast."""
    pass


async def _check_rate_limited(page) -> bool:
    try:
        toast = page.locator('[data-testid="toast"]').first
        if await toast.count() == 0:
            return False
        text = (await toast.inner_text()).lower()
        return "rate limited" in text
    except Exception:
        return False


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


def _parse_count(s: str) -> int | None:
    """Parse '3,341' / '1.2K' / '4.5M' / '12B' into an int."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMB]?)$", s, re.IGNORECASE)
    if not m:
        return None
    n = float(m.group(1))
    suffix = m.group(2).upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return int(n * mult)


async def get_profile_counts(page, username: str) -> dict:
    """Visit /{username} and scrape follower + following counts.

    Returns {"followers": int|None, "following": int|None}.
    """
    u = username.lstrip("@")
    url = f"{X_BASE}/{u}"
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(1.5, 3.0)
    try:
        await page.wait_for_selector(
            f'a[href="/{u}/verified_followers"], a[href="/{u}/followers"], a[href="/{u}/following"]',
            timeout=10_000,
        )
    except Exception:
        pass

    async def _read(href_candidates):
        for href in href_candidates:
            loc = page.locator(f'a[href="{href}"]').first
            try:
                if await loc.count() == 0:
                    continue
                # The first span inside the anchor holds the visible number.
                span = loc.locator("span span").first
                if await span.count() == 0:
                    txt = (await loc.inner_text()).split()[0]
                else:
                    txt = await span.inner_text()
                n = _parse_count(txt)
                if n is not None:
                    return n
            except Exception:
                continue
        return None

    followers = await _read([f"/{u}/verified_followers", f"/{u}/followers"])
    following = await _read([f"/{u}/following"])
    return {"followers": followers, "following": following}


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


async def _find_profile_action_button(page, username: str | None = None):
    """Return the *profile header* follow/unfollow/pending button, or None.

    Profile pages also render follow buttons in sidebar "Who to follow" widgets
    and inline "You might like" carousels — selecting `[data-testid$=-follow]`
    naively would pick one of those. We anchor by aria-label including the
    target @username (the profile button uses "Follow @user" / "Following @user"
    / "Follow back @user"), then fall back to primaryColumn-scoped selectors.
    """
    if username:
        u = username.lstrip("@")
        # aria-label uses the user's display-cased handle; X tends to preserve
        # case but URL handles can differ. Match case-insensitively via XPath.
        xpath = (
            f'//button[contains(translate(@aria-label, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "@{u.lower()}")]'
        )
        loc = page.locator(f'xpath={xpath}').first
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            pass

    for sel in [
        '[data-testid="primaryColumn"] [data-testid$="-unfollow"]',
        '[data-testid="primaryColumn"] [data-testid$="-follow"]',
        '[data-testid="primaryColumn"] [data-testid$="-cancel"]',
    ]:
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


async def get_follow_state(page, profile_url: str) -> str:
    """Visit a profile and return 'follow', 'unfollow', 'pending', 'private', or 'unknown'."""
    username = username_from_url(profile_url)
    await page.goto(profile_url, wait_until="domcontentloaded")
    await human_delay(1.2, 2.4)
    if await _is_protected_profile(page):
        # Could still have a Follow/Pending button — check anyway.
        btn = await _find_profile_action_button(page, username)
        if btn:
            s = await _button_state(btn)
            if s != "unknown":
                return s
        return "private"
    btn = await _find_profile_action_button(page, username)
    if not btn:
        return "unknown"
    return await _button_state(btn)


async def follow_user(page, profile_url: str, skip_private: bool = True) -> dict:
    """Navigate to a profile and click Follow. Returns {ok, status, reason}."""
    result = await _follow_user_impl(page, profile_url, skip_private)
    _log_action("follow", profile_url, result)
    return result


async def _follow_user_impl(page, profile_url: str, skip_private: bool) -> dict:
    username = username_from_url(profile_url)
    await page.goto(profile_url, wait_until="domcontentloaded")
    await human_delay(1.5, 3.0)

    if skip_private and await _is_protected_profile(page):
        return {"ok": False, "status": "skipped", "reason": "private"}

    btn = await _find_profile_action_button(page, username)
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
    except Exception as e:
        return {"ok": False, "status": "error", "reason": f"click failed: {e}"}

    # Poll for either the rate-limit toast or a button transition.
    after_state = "unknown"
    for _ in range(10):
        await asyncio.sleep(0.7)
        if await _check_rate_limited(page):
            return {"ok": False, "status": "rate_limited",
                    "reason": "X rate-limited follow action"}
        after = await _find_profile_action_button(page, username)
        after_state = await _button_state(after) if after else "unknown"
        if after_state in ("unfollow", "pending"):
            return {"ok": True, "status": "followed", "reason": after_state}
    return {"ok": False, "status": "error", "reason": f"state after click: {after_state}"}


async def unfollow_user(page, profile_url: str) -> dict:
    result = await _unfollow_user_impl(page, profile_url)
    _log_action("unfollow", profile_url, result)
    return result


async def _unfollow_user_impl(page, profile_url: str) -> dict:
    username = username_from_url(profile_url)
    await page.goto(profile_url, wait_until="domcontentloaded")
    await human_delay(1.5, 3.0)

    btn = await _find_profile_action_button(page, username)
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

    after = await _find_profile_action_button(page, username)
    after_state = await _button_state(after) if after else "unknown"
    if after_state == "follow":
        return {"ok": True, "status": "unfollowed", "reason": ""}
    return {"ok": False, "status": "error", "reason": f"state after click: {after_state}"}
