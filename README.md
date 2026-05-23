# TwitterBot3

Playwright-driven X/Twitter follow/unfollow churn bot with a persistent
browser profile. Every churn run uploads its results to Supabase so progress
can be tracked over time from the
[`SoundCloudTwitterBotsDashboard`](../SoundCloudTwitterBotsDashboard) Next.js
dashboard.

## Setup

```bash
poetry install
poetry run playwright install chromium
```

Create a `.env` at the repo root (gitignored):

```env
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-jwt>
```

The service-role key is required because the bot writes to RLS-protected
tables. Never commit `.env` — it is in `.gitignore`.

## One-time login

```bash
poetry run python src/main.py login
```

A headful Chromium opens to x.com/login. Sign in (with 2FA if needed), then
close the window. The session is persisted in `data/browser_profile/`.

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

The `followers` / `following` commands scrape the same virtualized list X
shows in the browser — scrolling until no new rows appear:

![follower list page](images/follower-list.png)

Add `--debug` to dump HTML + screenshots into `data/debug/`.
Add `--headful` to watch it work.

## Churn flow

The main automation loop. Idempotent — re-reads `data/actions.log` on every
run, so duplicate follows are skipped and rate limits self-enforce.

```bash
# Dry-run first to see what it would do without touching anything
poetry run python src/main.py churn --dry-run

# For real
poetry run python src/main.py churn
```

What it does, in order:

1. Reads `data/actions.log` and checks hour/day follow caps. If either is
   already met, exits immediately.
2. **Unfollow phase** — anyone successfully followed more than
   `MAX_FOLLOW_AGE_DAYS` ago (and not yet unfollowed) gets unfollowed, up to
   `MAX_UNFOLLOWS_PER_RUN`.
3. **Discovery phase** — pulls the top `SEED_FOLLOWERS_TOP_X` of your
   followers, then for each of them pulls the top `PER_SEED_FOLLOWERS_TOP_Y`
   of *their* followers (2 layers deep). Filters out anyone already touched
   in the log and any private accounts.

   ![scraping a seed account's follower list](images/follower-list2.png)
4. Follows up to `FOLLOWS_PER_RUN_Z` of those candidates, sleeping
   `SECONDS_BETWEEN_FOLLOWS` between clicks, stopping early if it would
   exceed the hour/day caps.

All follow/unfollow attempts are appended as JSON lines to
`data/actions.log`.

### Config

All tunables live in **`src/config.py`** — edit that file to change your
handle, rate limits, follow age, batch sizes, and pacing. Defaults are
conservative.

Safe to run on a cron (e.g. hourly) since the log makes it idempotent.

## Supabase reporting

At the end of every non-dry-run `churn` invocation, `src/supabase_client.py`
posts to two PostgREST tables in the shared project
`rxwdtssnaymiebnhudix.supabase.co`:

- **`twitter_actions`** — every follow/unfollow attempt from this session
  (account, ts, action, status, ok, profile_url, username, reason). A unique
  constraint on `(account, ts, profile_url, action)` makes the upload
  idempotent.
- **`twitter_runs`** — one row per cron invocation summarising
  `session_followed`, `session_unfollowed`, current `profile_followers` /
  `profile_following`, and `exit_code`.

The `account` column is taken from `config.MY_USERNAME`, so this bot and any
sibling clone (e.g. `TwitterBot3-1` authed as a different X account) both
write to the same tables and the dashboard separates them by account.

Email reporting (Resend) was removed — the dashboard replaces it.

## Cron

A scheduled task `TwitterBot3-Churn` (created with `schtasks`) runs
`cron/run-churn.ps1` every 3 hours, which adds 0-2h random jitter and then
calls `poetry run python src/main.py churn`. Three sibling tasks
(`TwitterBot3-Churn`, `TwitterBot3-1-Churn`, `SoundCloudBot3-Churn`) are
staggered (`:00`, `:30`, `:15`) so they don't overlap.
