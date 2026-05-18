"""Resend-backed email report for churn runs.

Loads RESEND_API_KEY, EMAIL_FROM, EMAIL_TO from a project-root .env file.
Counts lifetime follow/unfollow stats from data/actions.log, counts cron
invocations from logs/cron-wrapper-*.log, renders email_template.html, and
posts to https://api.resend.com/emails via stdlib urllib (no extra deps).
"""

import json
import os
from datetime import datetime
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ACTIONS_LOG = ROOT / "data" / "actions.log"
PROFILE_COUNTS_FILE = ROOT / "data" / "profile_counts.json"
LOGS_DIR = ROOT / "logs"
TEMPLATE_PATH = Path(__file__).resolve().parent / "email_template.html"


def _load_last_counts() -> dict:
    if not PROFILE_COUNTS_FILE.exists():
        return {}
    try:
        return json.loads(PROFILE_COUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_last_counts(followers: int | None, following: int | None) -> None:
    PROFILE_COUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "followers": followers,
        "following": following,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    PROFILE_COUNTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _fmt_count(n) -> str:
    if n is None:
        return "—"
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _fmt_delta(curr, prev) -> str:
    if curr is None or prev is None:
        return "(—)"
    d = int(curr) - int(prev)
    if d > 0:
        return f"(+{d:,})"
    if d < 0:
        return f"({d:,})"
    return "(0)"


def _delta_color(curr, prev, positive_good: bool = True) -> str:
    if curr is None or prev is None:
        return "#71767b"
    d = int(curr) - int(prev)
    if d == 0:
        return "#71767b"
    good = (d > 0) == positive_good
    return "#00ba7c" if good else "#f4212e"


def load_env():
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _greeting() -> str:
    h = datetime.now().hour
    if h < 12:
        return "Good morning"
    if h < 18:
        return "Good afternoon"
    return "Good evening"


def _count_actions(action: str, status: str) -> int:
    if not ACTIONS_LOG.exists():
        return 0
    n = 0
    for line in ACTIONS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("action") == action and e.get("status") == status:
            n += 1
    return n


def _count_cron_runs() -> int:
    if not LOGS_DIR.exists():
        return 0
    return sum(1 for _ in LOGS_DIR.glob("cron-wrapper-*.log"))


def gather_stats(
    session_followed: int,
    session_unfollowed: int,
    followers: int | None = None,
    following: int | None = None,
) -> dict:
    last = _load_last_counts()
    prev_followers = last.get("followers")
    prev_following = last.get("following")
    return {
        "greeting": _greeting(),
        "now": datetime.now().strftime("%H:%M:%S %m/%d/%Y"),
        "session_followed": session_followed,
        "session_unfollowed": session_unfollowed,
        "total_followed": _count_actions("follow", "followed"),
        "total_unfollowed": _count_actions("unfollow", "unfollowed"),
        "cron_runs": _count_cron_runs(),
        "followers": _fmt_count(followers),
        "following": _fmt_count(following),
        "followers_delta": _fmt_delta(followers, prev_followers),
        "following_delta": _fmt_delta(following, prev_following),
        "followers_delta_color": _delta_color(followers, prev_followers, positive_good=True),
        "following_delta_color": _delta_color(following, prev_following, positive_good=True),
    }


def _account_handle() -> str:
    try:
        import config as _cfg
        return getattr(_cfg, "MY_USERNAME", "unknown")
    except Exception:
        return "unknown"


def render_html(stats: dict, name: str = "Matthew") -> str:
    tmpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    ctx = {**stats, "name": name, "account": _account_handle()}
    for key, val in ctx.items():
        tmpl = tmpl.replace("{{" + key + "}}", str(val))
    return tmpl


def send_email(html: str, subject: str | None = None) -> dict:
    if subject is None:
        subject = f"TwitterBot Report — @{_account_handle()}"
    load_env()
    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("EMAIL_FROM")
    recipient = os.environ.get("EMAIL_TO")
    if not (api_key and sender and recipient):
        return {"ok": False, "error": "missing env vars (RESEND_API_KEY / EMAIL_FROM / EMAIL_TO)"}

    body = json.dumps({
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": html,
    }).encode("utf-8")
    req = request.Request(
        "https://api.resend.com/emails",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TwitterBot3/1.0 (+https://github.com/matthewmiglio/TWITTERBOT3.0)",
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return {"ok": True, "status": resp.status, "body": resp.read().decode("utf-8", errors="ignore")}
    except error.HTTPError as e:
        return {"ok": False, "status": e.code, "body": e.read().decode("utf-8", errors="ignore")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_report(
    session_followed: int,
    session_unfollowed: int,
    followers: int | None = None,
    following: int | None = None,
    name: str = "Matthew",
) -> dict:
    stats = gather_stats(session_followed, session_unfollowed, followers=followers, following=following)
    html = render_html(stats, name=name)
    result = send_email(html)
    # Persist current counts so the next run can compute deltas. Only save when
    # we actually scraped numbers — otherwise we'd clobber valid history.
    if followers is not None or following is not None:
        _save_last_counts(followers, following)
    return result
