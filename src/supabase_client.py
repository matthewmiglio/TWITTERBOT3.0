"""Supabase upload for churn runs.

Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from project-root .env, then
POSTs JSON to PostgREST. No third-party deps -- stdlib urllib only.

BOT_KIND switches which pair of tables we write to:
    twitter    -> twitter_actions    + twitter_runs
    soundcloud -> soundcloud_actions + soundcloud_runs
"""

import json
import os
from pathlib import Path
from urllib import error, request

BOT_KIND = "twitter"   # twitter | soundcloud

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _load_env():
    # Project-local .env is authoritative -- overwrite anything stale in the
    # parent process env. (setdefault used to let stale OS-level vars shadow
    # our .env and caused 401s on PostgREST uploads.)
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def _conf() -> tuple[str, str]:
    _load_env()
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return url, key


def _post(path: str, body: list | dict, prefer: str) -> dict:
    url, key = _conf()
    if not url or not key:
        return {"ok": False, "error": "missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY"}
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        f"{url}/rest/v1/{path}",
        data=payload, method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": prefer,
        },
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return {"ok": True, "status": resp.status, "body": resp.read().decode("utf-8", errors="ignore")}
    except error.HTTPError as e:
        return {"ok": False, "status": e.code, "body": e.read().decode("utf-8", errors="ignore")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _actions_table() -> str:
    return "twitter_actions" if BOT_KIND == "twitter" else "soundcloud_actions"


def _runs_table() -> str:
    return "twitter_runs" if BOT_KIND == "twitter" else "soundcloud_runs"


def upload_actions(rows: list[dict]) -> dict:
    if not rows:
        return {"ok": True, "status": 200, "body": "no rows"}
    # Chunk in 500-row batches to keep request size reasonable.
    # PostgREST needs ?on_conflict=<cols> to know which unique constraint to
    # upsert against -- without it, merge-duplicates falls back to the PK (id)
    # and INSERT collides with the (account, ts, profile_url, action) unique
    # index, returning 409.
    path = _actions_table() + "?on_conflict=account,ts,profile_url,action"
    last = {"ok": True, "status": 200, "body": ""}
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = _post(path, chunk,
                  prefer="resolution=merge-duplicates,return=minimal")
        if not r.get("ok"):
            return r
        last = r
    return last


def upload_run(run: dict) -> dict:
    path = _runs_table() + "?on_conflict=account,started_at"
    return _post(path, [run],
                 prefer="resolution=merge-duplicates,return=minimal")


def upload_error(err: dict) -> dict:
    """Best-effort error row insert. Never raises -- callers should ignore
    failures since the bot itself is already in an error path.

    Caller supplies: account, source, kind, message, and optionally
    exit_code, traceback, run_started_at, context. The `bot` column is
    derived from BOT_KIND so the dashboard can pair errors with runs.
    """
    body = {**err, "bot": BOT_KIND}
    path = "bot_errors?on_conflict=bot,account,ts,source,kind,message"
    return _post(path, [body],
                 prefer="resolution=merge-duplicates,return=minimal")
