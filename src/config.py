"""Churn flow configuration. Edit these values to tune the bot."""

# Your X handle (no @). Used as the seed account whose followers we mine.
MY_USERNAME = "whatsaplat"

# --- Rate limits ---
# Hard caps on follow actions. If either is exceeded based on actions.log,
# the churn run quits immediately (idempotent — safe to re-run).
MAX_FOLLOWS_PER_HOUR = 8
MAX_FOLLOWS_PER_DAY = 30

# --- Unfollow policy ---
# Unfollow anyone we followed more than this many days ago (per actions.log).
MAX_FOLLOW_AGE_DAYS = 10
# Cap unfollows per run so a single churn doesn't dump hundreds at once.
MAX_UNFOLLOWS_PER_RUN = 10

# --- Discovery (2-layer-deep follower mining) ---
# Layer 1: pull this many of MY_USERNAME's followers as "seeds".
SEED_FOLLOWERS_TOP_X = 25
# Layer 2: for each seed, pull this many of their followers as candidates.
PER_SEED_FOLLOWERS_TOP_Y = 20
# How many candidates to actually follow this run (subject to rate limits).
FOLLOWS_PER_RUN_Z = 8

# --- Pacing ---
# Extra seconds to sleep between follow clicks, on top of the human_delay
# inside follow_user(). Helps avoid burst-detection.
SECONDS_BETWEEN_FOLLOWS = (30, 90)
SECONDS_BETWEEN_UNFOLLOWS = (15, 40)
