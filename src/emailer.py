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
LOGS_DIR = ROOT / "logs"
TEMPLATE_PATH = Path(__file__).resolve().parent / "email_template.html"


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


def gather_stats(session_followed: int, session_unfollowed: int) -> dict:
    return {
        "greeting": _greeting(),
        "now": datetime.now().strftime("%H:%M:%S %m/%d/%Y"),
        "session_followed": session_followed,
        "session_unfollowed": session_unfollowed,
        "total_followed": _count_actions("follow", "followed"),
        "total_unfollowed": _count_actions("unfollow", "unfollowed"),
        "cron_runs": _count_cron_runs(),
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


def send_report(session_followed: int, session_unfollowed: int, name: str = "Matthew") -> dict:
    stats = gather_stats(session_followed, session_unfollowed)
    html = render_html(stats, name=name)
    return send_email(html)
