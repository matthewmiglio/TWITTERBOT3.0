# TwitterBot3

Playwright-driven X/Twitter bot with a persistent browser profile.

## Setup

```bash
poetry install
poetry run playwright install chromium
```

## One-time login

```bash
poetry run python src/main.py login
```

A headful Chromium opens to x.com/login. Sign in (with 2FA if needed), then close the window. The session is now persisted in `data/browser_profile/`.

## Commands

```bash
# List followers of a user (prints profile URLs; skips private accounts)
poetry run python src/main.py followers <username>

# List who a user follows
poetry run python src/main.py following <username>

# Follow a user by profile URL
poetry run python src/main.py follow https://x.com/<username>

# Unfollow a user by profile URL
poetry run python src/main.py unfollow https://x.com/<username>
```

Add `--debug` to dump HTML + screenshots into `data/debug/`.
Add `--headful` to watch it work.
