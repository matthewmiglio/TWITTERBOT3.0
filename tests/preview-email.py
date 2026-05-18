"""Render the email template with realistic-looking sample data and open it
in the default browser for visual QA. Does NOT call Resend."""

import os
import sys
import tempfile
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from emailer import render_html  # noqa: E402


SAMPLE = {
    "greeting": "Good afternoon",
    "now": "16:34:18 05/17/2026",
    "session_followed": 7,
    "session_unfollowed": 2,
    "total_followed": 23,
    "total_unfollowed": 4,
    "cron_runs": 12,
    "account": "whatsaplat",
}


def main():
    html = render_html(SAMPLE, name="Matthew")
    fd, path = tempfile.mkstemp(suffix=".html", prefix="email-preview-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[preview] wrote {path}")
    webbrowser.open(f"file:///{path.replace(os.sep, '/')}")


if __name__ == "__main__":
    main()
