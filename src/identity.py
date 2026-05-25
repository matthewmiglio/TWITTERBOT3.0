"""Account identity for this clone.

The X handle this clone acts as is *per-clone state*, not source code. It
lives in data/profile.json next to the browser profile so the tracked
source files are byte-identical across clones -- only the gitignored
data/ directory differs.

Bootstrap:
    python src/main.py login          # interactive sign-in
    python src/main.py whoami         # scrape handle from authed browser -> profile.json
or:
    python src/main.py whoami --username whatsaplat
"""

import json
import os
from pathlib import Path

PROFILE_FILE = Path(__file__).resolve().parent.parent / "data" / "profile.json"

_cached: str | None = None


def get_username() -> str:
    """Return the bot's own handle. Reads data/profile.json, falls back to
    the MY_USERNAME env var, raises if neither is set."""
    global _cached
    if _cached:
        return _cached
    if PROFILE_FILE.exists():
        try:
            data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            u = (data.get("username") or "").strip().lstrip("@")
            if u:
                _cached = u
                return u
        except Exception:
            pass
    env = (os.environ.get("MY_USERNAME") or "").strip().lstrip("@")
    if env:
        _cached = env
        return env
    raise RuntimeError(
        f"No account identity. Write {PROFILE_FILE} with "
        '{"username": "<your-handle>"} or run: python src/main.py whoami --username <handle>'
    )


def set_username(username: str) -> None:
    """Persist the username into data/profile.json. Used by the login /
    whoami flow."""
    global _cached
    username = (username or "").strip().lstrip("@")
    if not username:
        raise ValueError("username is empty")
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if PROFILE_FILE.exists():
        try:
            existing = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing["username"] = username
    PROFILE_FILE.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    _cached = username
