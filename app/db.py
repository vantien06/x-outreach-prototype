from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    x_user_id TEXT NOT NULL UNIQUE,
    handle TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    access_token_enc TEXT NOT NULL DEFAULT '',
    refresh_token_enc TEXT NOT NULL DEFAULT '',
    token_expires_at TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    needs_reauth INTEGER NOT NULL DEFAULT 0,
    backoff_until TEXT,
    last_reply_poll_at TEXT,
    last_poll_error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    code_verifier TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('segment','seeds')),
    segment TEXT NOT NULL DEFAULT '',
    seeds TEXT NOT NULL DEFAULT '',
    pitch TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'discovering',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    x_user_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    followers INTEGER NOT NULL DEFAULT 0,
    can_dm INTEGER,
    source TEXT NOT NULL DEFAULT '',
    fit_score INTEGER,
    fit_category TEXT,
    fit_reason TEXT,
    fit_evidence_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    UNIQUE (account_id, x_user_id)
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('bio','post')),
    ref_id TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    posted_at TEXT,
    UNIQUE (candidate_id, kind, ref_id)
);
-- One row per (sending account, recipient), ever. This constraint is the
-- last line of defence against double-messaging.
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    recipient_x_user_id TEXT NOT NULL,
    recipient_handle TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('drafted','approved','sending','sent','blocked','needs_check','skipped')),
    text TEXT NOT NULL DEFAULT '',
    claims TEXT NOT NULL DEFAULT '[]',
    grounding_note TEXT NOT NULL DEFAULT '',
    edited INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    dm_event_id TEXT,
    dm_conversation_id TEXT,
    approved_at TEXT,
    sending_at TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, recipient_x_user_id)
);
CREATE INDEX IF NOT EXISTS outreach_state ON outreach (account_id, state);
CREATE TABLE IF NOT EXISTS dm_events (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    sender_x_user_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('in','out')),
    text TEXT NOT NULL DEFAULT '',
    event_at TEXT,
    outreach_id INTEGER REFERENCES outreach(id) ON DELETE CASCADE,
    label TEXT,
    summary TEXT,
    handled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, event_id)
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    id INTEGER PRIMARY KEY,
    account_id INTEGER,
    kind TEXT NOT NULL,
    units INTEGER NOT NULL,
    est_cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_day ON usage_ledger (account_id, kind, created_at);
-- Survives "disconnect and delete my data": only a SHA-256 of the
-- (sender id, recipient id) pair, so a reconnect can never re-message someone.
CREATE TABLE IF NOT EXISTS contact_tombstones (
    pair_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
-- Dry-run only: the simulated X DM inbox.
CREATE TABLE IF NOT EXISTS mock_dm_events (
    id INTEGER PRIMARY KEY,
    owner_x_user_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    visible_at TEXT NOT NULL
);
"""

# Columns added after the first version. Applied with ALTER TABLE so an existing
# database keeps its data.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("outreach", "last_reply_check_at", "TEXT"),
    ("outreach", "reply_checks", "INTEGER NOT NULL DEFAULT 0"),
    ("mock_dm_events", "kind", "TEXT NOT NULL DEFAULT 'message'"),
    ("candidates", "avatar_url", "TEXT NOT NULL DEFAULT ''"),
    ("campaigns", "depth", "TEXT NOT NULL DEFAULT 'quick'"),
]


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def day_start() -> str:
    return iso(now().replace(hour=0, minute=0, second=0, microsecond=0))


def seconds_ago(n: float) -> str:
    return iso(now() - timedelta(seconds=n))


def connect() -> sqlite3.Connection:
    path = Path(get_settings().database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """Write transaction that takes the write lock up front."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
