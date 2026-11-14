"""Usage ledger, per-account daily limits and the global budget ceiling."""
from __future__ import annotations

import sqlite3

from ..config import get_settings
from ..db import day_start, db, iso


class LimitReached(Exception):
    """A limit blocks this action. The message is shown to the operator as-is."""


def _unit_cost(kind: str) -> float:
    s = get_settings()
    return {
        "x_post_read": s.cost_x_post_read,
        "x_user_read": s.cost_x_user_read,
        "x_dm_send": s.cost_x_dm_send,
        "x_dm_read": s.cost_x_dm_read,
        "x_chat_read": s.cost_x_chat_read,
        "llm_fast": s.cost_llm_fast,
        "llm_draft": s.cost_llm_draft,
    }[kind]


def record(account_id: int | None, kind: str, units: int = 1) -> None:
    if units <= 0:
        return
    # Dry-run makes no billable calls; the ledger still counts units so the UI is honest about volume.
    cost = 0.0 if get_settings().dry_run else _unit_cost(kind) * units
    with db() as conn:
        conn.execute(
            "INSERT INTO usage_ledger (account_id, kind, units, est_cost_usd, created_at) VALUES (?,?,?,?,?)",
            (account_id, kind, units, cost, iso()),
        )


def chat_reads_since(conn: sqlite3.Connection, account_id: int, since_iso: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(units),0) FROM usage_ledger WHERE account_id=? AND kind='x_chat_read' AND created_at>=?",
        (account_id, since_iso),
    ).fetchone()[0]


def spent_total(conn: sqlite3.Connection) -> float:
    return conn.execute("SELECT COALESCE(SUM(est_cost_usd),0) FROM usage_ledger").fetchone()[0]


def budget_locked(conn: sqlite3.Connection) -> bool:
    ceiling = get_settings().global_budget_usd
    return ceiling > 0 and spent_total(conn) >= ceiling


def check_budget(conn: sqlite3.Connection) -> None:
    if budget_locked(conn):
        raise LimitReached(
            "This tool has reached its usage limit for now, so finding people, drafting and sending are paused. "
            "Please contact the person who runs it."
        )


def sends_today(conn: sqlite3.Connection, account_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM outreach WHERE account_id=? AND sending_at>=?"
        " AND state IN ('sending','sent','needs_check')",
        (account_id, day_start()),
    ).fetchone()[0]


def candidates_today(conn: sqlite3.Connection, account_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(units),0) FROM usage_ledger WHERE account_id=? AND kind='llm_fast_candidate' AND created_at>=?",
        (account_id, day_start()),
    ).fetchone()[0]


def candidates_left(conn: sqlite3.Connection, account_id: int) -> int | None:
    cap = get_settings().daily_candidate_cap
    return None if cap <= 0 else max(0, cap - candidates_today(conn, account_id))


def note_candidate_scored(account_id: int) -> None:
    """Counts toward the daily candidate cap (survives data the user later deletes from view)."""
    with db() as conn:
        conn.execute(
            "INSERT INTO usage_ledger (account_id, kind, units, est_cost_usd, created_at) VALUES (?,?,?,?,?)",
            (account_id, "llm_fast_candidate", 1, 0.0, iso()),
        )


def summary(conn: sqlite3.Connection, account_id: int) -> dict:
    s = get_settings()
    rows = conn.execute(
        "SELECT kind, SUM(units) u, SUM(est_cost_usd) c FROM usage_ledger"
        " WHERE account_id=? AND kind!='llm_fast_candidate' GROUP BY kind",
        (account_id,),
    ).fetchall()
    return {
        "rows": [dict(r) for r in rows],
        "account_cost": sum(r["c"] for r in rows),
        "sends_today": sends_today(conn, account_id),
        "send_cap": s.daily_send_cap,
        "candidates_today": candidates_today(conn, account_id),
        "candidate_cap": s.daily_candidate_cap,
        "global_spent": spent_total(conn),
        "global_budget": s.global_budget_usd,
        "locked": budget_locked(conn),
    }
