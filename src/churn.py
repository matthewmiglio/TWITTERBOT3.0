"""Churn flow: unfollow stale follows + discover and follow new users.

Idempotent — re-reading actions.log on every run means duplicate follows are
skipped and rate limits self-enforce. Safe to schedule on a cron.

Flow
----
1. Load actions.log → compute follow state per user + rate-limit windows.
2. If MAX_FOLLOWS_PER_HOUR or MAX_FOLLOWS_PER_DAY already met → exit early.
3. Unfollow phase: anyone we followed > MAX_FOLLOW_AGE_DAYS ago (and haven't
   already unfollowed), up to MAX_UNFOLLOWS_PER_RUN.
4. Discovery phase: SEED_FOLLOWERS_TOP_X of my followers → for each, pull
   PER_SEED_FOLLOWERS_TOP_Y of *their* followers → dedupe, filter, follow
   up to FOLLOWS_PER_RUN_Z (also respecting hour/day caps).
"""

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

import config
from browser import (
    launch_browser,
    check_login_status,
    set_debug,
    human_delay,
    ACTIONS_LOG,
)
from twitter import (
    list_followers,
    follow_user,
    unfollow_user,
    username_from_url,
    get_follow_state,
    get_profile_counts,
)
from supabase_client import upload_actions, upload_run

X_BASE = "https://x.com"
LOGS_DIR = os.path.join(os.path.dirname(ACTIONS_LOG), "..", "logs")
LOGS_DIR = os.path.abspath(LOGS_DIR)


class _SessionLogger:
    """Tee print()-style messages to stdout AND a per-session log file."""

    def __init__(self, prefix: str):
        os.makedirs(LOGS_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(LOGS_DIR, f"{prefix}-{ts}.log")
        self._fh = open(self.path, "a", encoding="utf-8")

    def log(self, msg: str = ""):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}"
        print(msg)
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


# ----------------------------- log parsing -----------------------------

def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Python <3.11 doesn't accept "Z"; ours uses %z, but be defensive.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def load_actions() -> list[dict]:
    if not os.path.exists(ACTIONS_LOG):
        return []
    out = []
    with open(ACTIONS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            entry["_ts"] = _parse_ts(entry.get("timestamp"))
            out.append(entry)
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def successful_follow_count_since(actions: list[dict], since: datetime) -> int:
    n = 0
    for a in actions:
        if a.get("action") != "follow":
            continue
        if a.get("status") != "followed":
            continue
        ts = a.get("_ts")
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= since:
            n += 1
    return n


def already_acted_usernames(actions: list[dict]) -> set[str]:
    """Anyone we've ever tried to follow OR successfully unfollowed — don't re-follow."""
    s = set()
    for a in actions:
        u = (a.get("username") or "").lower()
        if not u:
            continue
        if a.get("action") == "follow":
            s.add(u)
        if a.get("action") == "unfollow" and a.get("status") == "unfollowed":
            s.add(u)  # we just unfollowed — don't immediately re-follow
    return s


def stale_follows(actions: list[dict], max_age_days: int) -> list[dict]:
    """Users we successfully followed > max_age_days ago and haven't unfollowed since.

    Returns list of {username, profile_url, followed_at}, oldest first.
    """
    now = _now()
    cutoff = now - timedelta(days=max_age_days)

    # Walk chronologically; track last action per user.
    sorted_actions = sorted(
        [a for a in actions if a.get("_ts") is not None],
        key=lambda a: a["_ts"],
    )
    state: dict[str, dict] = {}  # username -> {state, ts, profile_url}
    for a in sorted_actions:
        u = (a.get("username") or "").lower()
        if not u:
            continue
        action = a.get("action")
        status = a.get("status")
        if action == "follow" and status == "followed":
            state[u] = {
                "state": "following",
                "ts": a["_ts"],
                "profile_url": a.get("profile_url") or f"{X_BASE}/{u}",
            }
        elif action == "unfollow" and status == "unfollowed":
            state[u] = {
                "state": "unfollowed",
                "ts": a["_ts"],
                "profile_url": a.get("profile_url") or f"{X_BASE}/{u}",
            }

    stale = []
    for u, info in state.items():
        if info["state"] != "following":
            continue
        ts = info["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts <= cutoff:
            stale.append({
                "username": u,
                "profile_url": info["profile_url"],
                "followed_at": ts,
            })
    stale.sort(key=lambda x: x["followed_at"])
    return stale


# ----------------------------- main flow -----------------------------

async def _sleep_between(window):
    lo, hi = window
    await asyncio.sleep(random.uniform(lo, hi))


async def run_reconcile(headful: bool = False) -> int:
    """Walk every action-log entry with status='error', visit the profile, and
    rewrite the entry to reflect the actual current follow state. Fixes history
    written by the pre-fix scoped-button-finder bug so stale-follow detection
    correctly knows who we follow."""
    if not os.path.exists(ACTIONS_LOG):
        print("[reconcile] no log file at", ACTIONS_LOG)
        return 0

    with open(ACTIONS_LOG, "r", encoding="utf-8") as f:
        raw_lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    entries = []
    for ln in raw_lines:
        try:
            entries.append(json.loads(ln))
        except Exception:
            entries.append(None)

    targets = [i for i, e in enumerate(entries)
               if e and e.get("action") == "follow" and e.get("status") == "error"]
    print(f"[reconcile] {len(targets)} follow entries with status=error to verify")
    if not targets:
        return 0

    pw, context, page = await launch_browser(headless=not headful)
    try:
        await page.goto(f"{X_BASE}/home", wait_until="domcontentloaded")
        if not await check_login_status(page):
            print("[reconcile] not logged in — run: python src/main.py login")
            return 2

        changed = 0
        for i in targets:
            e = entries[i]
            url = e.get("profile_url")
            print(f"[reconcile] checking {url}")
            state = await get_follow_state(page, url)
            print(f"             actual state -> {state}")
            if state in ("unfollow", "pending"):
                # We are actually following (or have a pending request).
                e["ok"] = True
                e["status"] = "followed"
                e["reason"] = f"reconciled ({state})"
                changed += 1
            elif state == "follow":
                e["reason"] = "reconciled (still not following)"
            elif state == "private":
                e["reason"] = "reconciled (account private)"
            else:
                e["reason"] = f"reconciled (state={state})"
            await human_delay(1.5, 3.5)

        # Rewrite log file with updated entries.
        with open(ACTIONS_LOG, "w", encoding="utf-8") as f:
            for orig_line, e in zip(raw_lines, entries):
                if e is None:
                    f.write(orig_line + "\n")
                else:
                    f.write(json.dumps(e) + "\n")
        print(f"[reconcile] done. updated {changed} entries to status=followed.")
        return 0
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()


async def run_churn(dry_run: bool = False, headful: bool = False) -> int:
    logger = _SessionLogger("churn")
    log = logger.log
    started_at = _now()
    exit_code = 1
    stats = {"followed": 0, "unfollowed": 0, "followers": None, "following": None,
             "followed_urls": [], "unfollowed_urls": []}
    try:
        exit_code = await _run_churn_impl(log, stats, dry_run=dry_run, headful=headful)
    finally:
        log(f"[churn] session log written to {logger.path}")
        if not dry_run:
            log(f"[churn] profile counts: followers={stats['followers']} following={stats['following']}")
            new_rows = [
                {
                    "account":     config.MY_USERNAME,
                    "ts":          a["timestamp"],
                    "action":      a.get("action"),
                    "status":      a.get("status"),
                    "ok":          bool(a.get("ok")),
                    "profile_url": a.get("profile_url"),
                    "username":    a.get("username"),
                    "reason":      a.get("reason") or "",
                }
                for a in load_actions()
                if a.get("_ts") is not None and a["_ts"] >= started_at - timedelta(minutes=1)
            ]
            r1 = upload_actions(new_rows)
            log(f"[churn] supabase actions upload: ok={r1.get('ok')} status={r1.get('status') or r1.get('error')} rows={len(new_rows)}")
            r2 = upload_run({
                "account":            config.MY_USERNAME,
                "started_at":         started_at.isoformat(),
                "finished_at":        _now().isoformat(),
                "session_followed":   stats["followed"],
                "session_unfollowed": stats["unfollowed"],
                "profile_followers":  stats["followers"],
                "profile_following":  stats["following"],
                "exit_code":          exit_code,
            })
            log(f"[churn] supabase run upload:     ok={r2.get('ok')} status={r2.get('status') or r2.get('error')}")
        logger.close()
    return exit_code


async def _run_churn_impl(log, stats: dict, dry_run: bool, headful: bool) -> int:
    actions = load_actions()
    now = _now()
    follows_last_hour = successful_follow_count_since(actions, now - timedelta(hours=1))
    follows_last_day = successful_follow_count_since(actions, now - timedelta(days=1))

    log(f"[churn] dry_run={dry_run} headful={headful}")
    log(f"[churn] follows in last hour: {follows_last_hour}/{config.MAX_FOLLOWS_PER_HOUR}")
    log(f"[churn] follows in last day:  {follows_last_day}/{config.MAX_FOLLOWS_PER_DAY}")

    if follows_last_hour >= config.MAX_FOLLOWS_PER_HOUR:
        log("[churn] hourly follow cap reached -- quitting.")
        return 0
    if follows_last_day >= config.MAX_FOLLOWS_PER_DAY:
        log("[churn] daily follow cap reached -- quitting.")
        return 0

    hour_room = config.MAX_FOLLOWS_PER_HOUR - follows_last_hour
    day_room = config.MAX_FOLLOWS_PER_DAY - follows_last_day
    follow_budget = min(config.FOLLOWS_PER_RUN_Z, hour_room, day_room)

    stale = stale_follows(actions, config.MAX_FOLLOW_AGE_DAYS)
    unfollow_budget = min(config.MAX_UNFOLLOWS_PER_RUN, len(stale))
    log(f"[churn] stale follows eligible to unfollow: {len(stale)} (will do up to {unfollow_budget})")
    log(f"[churn] follow budget this run: {follow_budget}")

    if dry_run:
        log("[churn] --- DRY RUN ---")
        log(f"[churn] would unfollow {unfollow_budget}:")
        for s in stale[:unfollow_budget]:
            age = (now - s["followed_at"]).days
            log(f"          {s['profile_url']}  (followed {age}d ago)")

    pw, context, page = await launch_browser(headless=not headful)
    try:
        await page.goto(f"{X_BASE}/home", wait_until="domcontentloaded")
        if not await check_login_status(page):
            log("[churn] not logged in -- run: python src/main.py login")
            return 2

        # --------- Profile counts ---------
        try:
            counts = await get_profile_counts(page, config.MY_USERNAME)
            stats["followers"] = counts.get("followers")
            stats["following"] = counts.get("following")
            log(f"[churn] profile @{config.MY_USERNAME}: followers={stats['followers']} following={stats['following']}")
        except Exception as e:
            log(f"[churn] profile counts scrape failed: {e}")

        # --------- Unfollow phase ---------
        for s in stale[:unfollow_budget]:
            age = (now - s["followed_at"]).days
            if dry_run:
                continue
            log(f"[churn] unfollow {s['profile_url']} (followed {age}d ago)")
            result = await unfollow_user(page, s["profile_url"])
            log(f"          ->{result}")
            if result.get("status") == "unfollowed":
                stats["unfollowed"] += 1
                stats["unfollowed_urls"].append(s["profile_url"])
            await _sleep_between(config.SECONDS_BETWEEN_UNFOLLOWS)

        # --------- Discovery phase ---------
        already = already_acted_usernames(load_actions())  # re-read after unfollows
        already.add(config.MY_USERNAME.lower())

        log(f"[churn] mining followers of @{config.MY_USERNAME} (top {config.SEED_FOLLOWERS_TOP_X})")
        seeds = await list_followers(page, config.MY_USERNAME, max_users=config.SEED_FOLLOWERS_TOP_X)
        seeds = [s for s in seeds if not s["is_private"]]
        log(f"[churn] got {len(seeds)} seed accounts")

        candidates: list[dict] = []
        seen = set()
        for seed in seeds:
            log(f"[churn] mining followers of @{seed['username']} (top {config.PER_SEED_FOLLOWERS_TOP_Y})")
            sub = await list_followers(page, seed["username"], max_users=config.PER_SEED_FOLLOWERS_TOP_Y)
            for r in sub:
                u = r["username"].lower()
                if u in seen or u in already or r["is_private"]:
                    continue
                seen.add(u)
                candidates.append(r)
            await human_delay(1.0, 2.5)

        log(f"[churn] {len(candidates)} fresh candidates after dedup/filter")

        if dry_run:
            log(f"[churn] would follow up to {follow_budget}:")
            for c in candidates[:follow_budget]:
                log(f"          {c['profile_url']}")
            return 0

        for c in candidates:
            if stats["followed"] >= follow_budget:
                break
            # Re-check live rate limits before each click in case of long runs.
            acts = load_actions()
            if successful_follow_count_since(acts, _now() - timedelta(hours=1)) >= config.MAX_FOLLOWS_PER_HOUR:
                log("[churn] hourly cap hit mid-run -- stopping follows.")
                break
            if successful_follow_count_since(acts, _now() - timedelta(days=1)) >= config.MAX_FOLLOWS_PER_DAY:
                log("[churn] daily cap hit mid-run -- stopping follows.")
                break

            log(f"[churn] follow {c['profile_url']}")
            result = await follow_user(page, c["profile_url"], skip_private=True)
            log(f"          ->{result}")
            if result.get("status") == "rate_limited":
                log("[churn] X says we're rate-limited. Stopping follow loop for this session.")
                break
            if result.get("status") == "followed":
                stats["followed"] += 1
                stats["followed_urls"].append(c["profile_url"])
            await _sleep_between(config.SECONDS_BETWEEN_FOLLOWS)

        log(f"[churn] done. unfollowed={stats['unfollowed']}, followed={stats['followed']}")
        return 0
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()
