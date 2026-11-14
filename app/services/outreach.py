"""Outreach state machine, drafting and the send path.

The rule this module exists to enforce: a recipient gets at most one message
from a given account, ever.

  1. UNIQUE (account_id, recipient_x_user_id) on the outreach table: there can
     only be one outreach row per person.
  2. A row is claimed for sending with a conditional UPDATE (approved -> sending)
     inside a write transaction, so two workers can never both claim it, and the
     'sending' state is on disk before X is called.
  3. Only a definitive "not sent" answer from X returns a row to the queue. A
     timeout or 5xx parks it in needs_check, which only a human (or matching DM
     history) can resolve. The worker never re-sends on its own.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta

from ..config import get_settings
from ..db import db, iso, now, parse_iso, seconds_ago, tx
from ..llm.base import EvidenceItem, LLMClient, LLMError, Person
from ..x.base import XAmbiguous, XAuthError, XClient, XError, XForbidden, XPost, XRateLimited, XUser
from . import quota

MAX_DM_CHARS = 1000
MAX_PRECHECK_ATTEMPTS = 3

# from-state -> allowed to-states
TRANSITIONS: dict[str, set[str]] = {
    "drafted": {"approved", "skipped"},
    "approved": {"drafted", "sending", "skipped"},
    "sending": {"sent", "blocked", "needs_check", "approved"},
    "needs_check": {"sent", "approved"},  # human decision, or DM history proving delivery
    "skipped": {"drafted"},
    "sent": set(),
    "blocked": set(),
}


class InvalidTransition(Exception):
    pass


def transition(conn: sqlite3.Connection, outreach_id: int, frm: str, to: str, **fields) -> bool:
    """Move a row frm -> to only if it is currently in frm. Returns False if it was not."""
    if to not in TRANSITIONS.get(frm, set()):
        raise InvalidTransition(f"{frm} -> {to} is not allowed")
    fields = {"state": to, "updated_at": iso(), **fields}
    sets = ", ".join(f"{k}=?" for k in fields)
    cur = conn.execute(
        f"UPDATE outreach SET {sets} WHERE id=? AND state=?", (*fields.values(), outreach_id, frm)
    )
    return cur.rowcount == 1


def pair_hash(sender_x_user_id: str, recipient_x_user_id: str) -> str:
    return hashlib.sha256(f"{sender_x_user_id}:{recipient_x_user_id}".encode()).hexdigest()


# ---------------------------------------------------------------- drafting

def _evidence_items(conn: sqlite3.Connection, candidate_id: int) -> list[EvidenceItem]:
    rows = conn.execute("SELECT * FROM evidence WHERE candidate_id=? ORDER BY kind, id", (candidate_id,)).fetchall()
    return [EvidenceItem(id=f"E{r['id']}", kind=r["kind"], text=r["text"], url=r["url"]) for r in rows]


def add_post_evidence(conn: sqlite3.Connection, account_id: int, candidate_id: int, posts: list[XPost]) -> None:
    for p in posts:
        conn.execute(
            "INSERT OR IGNORE INTO evidence (account_id, candidate_id, kind, ref_id, url, text, posted_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (account_id, candidate_id, "post", p.id, p.url, p.text, p.created_at),
        )


def check_grounding(message: str, claims: list[dict], valid_ids: set[str]) -> tuple[list[dict], str]:
    """Keep only claims that cite stored evidence; describe anything that does not check out."""
    kept, problems = [], []
    for c in claims:
        if c.get("evidence_id") in valid_ids:
            kept.append(c)
        else:
            problems.append(f"Claim without stored evidence: \"{c.get('claim', '')}\"")
    if not kept:
        problems.append("This draft cites no evidence. Check it does not assert anything about the recipient.")
    if len(message) > MAX_DM_CHARS:
        problems.append(f"Draft is {len(message)} characters; the limit here is {MAX_DM_CHARS}.")
    return kept, " ".join(problems)


def create_draft(account, candidate_id: int, llm: LLMClient, x: XClient) -> int:
    """Create (or return) the single outreach row for this candidate. Returns outreach id."""
    account_id = account["id"]
    with db() as conn:
        cand = conn.execute(
            "SELECT c.*, k.pitch FROM candidates c JOIN campaigns k ON k.id=c.campaign_id"
            " WHERE c.id=? AND c.account_id=?", (candidate_id, account_id),
        ).fetchone()
        if cand is None:
            raise LookupError("candidate not found")
        existing = conn.execute(
            "SELECT id FROM outreach WHERE account_id=? AND recipient_x_user_id=?",
            (account_id, cand["x_user_id"]),
        ).fetchone()
        if existing:
            return existing["id"]
        tomb = conn.execute(
            "SELECT 1 FROM contact_tombstones WHERE pair_hash=?",
            (pair_hash(account["x_user_id"], cand["x_user_id"]),),
        ).fetchone()
        if not tomb:
            quota.check_budget(conn)
        has_posts = conn.execute(
            "SELECT 1 FROM evidence WHERE candidate_id=? AND kind='post'", (candidate_id,)
        ).fetchone()

    ts = iso()
    base = (account_id, cand["campaign_id"], candidate_id, cand["x_user_id"], cand["handle"])
    if tomb:
        # Contacted before this account's data was deleted: never draft again.
        with db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO outreach (account_id, campaign_id, candidate_id, recipient_x_user_id,"
                " recipient_handle, state, reason, created_at, updated_at) VALUES (?,?,?,?,?, 'blocked', ?, ?, ?)",
                (*base, "Already contacted from this account before its data was deleted.", ts, ts),
            )
            conn.execute("UPDATE candidates SET status='drafted' WHERE id=?", (candidate_id,))
            return cur.lastrowid or _existing_id(conn, account_id, cand["x_user_id"])

    if not has_posts:
        # Seed-derived candidates only have a bio so far; fetch a few posts for real evidence.
        try:
            posts = x.user_posts(XUser(id=cand["x_user_id"], handle=cand["handle"]), 5)
        except (XError, XAmbiguous):
            posts = []
        with db() as conn:
            add_post_evidence(conn, account_id, candidate_id, posts)

    with db() as conn:
        evidence = _evidence_items(conn, candidate_id)
    person = Person(handle=cand["handle"], name=cand["name"], bio=cand["bio"], evidence=evidence)
    draft = llm.draft_dm(cand["pitch"], account["handle"], person)  # LLMError propagates to the caller
    quota.record(account_id, "llm_draft", 1)
    claims, note = check_grounding(
        draft.message, [c.model_dump() for c in draft.claims], {e.id for e in evidence}
    )
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO outreach (account_id, campaign_id, candidate_id, recipient_x_user_id,"
            " recipient_handle, state, text, claims, grounding_note, created_at, updated_at)"
            " VALUES (?,?,?,?,?, 'drafted', ?, ?, ?, ?, ?)",
            (*base, draft.message.strip(), json.dumps(claims), note, ts, ts),
        )
        conn.execute("UPDATE candidates SET status='drafted' WHERE id=?", (candidate_id,))
        return cur.lastrowid if cur.rowcount else _existing_id(conn, account_id, cand["x_user_id"])


def _existing_id(conn: sqlite3.Connection, account_id: int, recipient: str) -> int:
    return conn.execute(
        "SELECT id FROM outreach WHERE account_id=? AND recipient_x_user_id=?", (account_id, recipient)
    ).fetchone()["id"]


# ---------------------------------------------------------------- review actions

def save_text(account_id: int, outreach_id: int, text: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE outreach SET text=?, edited=1, updated_at=? WHERE id=? AND account_id=? AND state='drafted'"
            " AND text!=?", (text.strip(), iso(), outreach_id, account_id, text.strip()),
        )


def approve(account_id: int, outreach_id: int) -> str | None:
    """Returns an error message, or None on success."""
    with db() as conn:
        row = conn.execute("SELECT * FROM outreach WHERE id=? AND account_id=?", (outreach_id, account_id)).fetchone()
        if row is None:
            return "Not found."
        if not row["text"].strip():
            return "The message is empty."
        if len(row["text"]) > MAX_DM_CHARS:
            return f"The message is {len(row['text'])} characters; keep it under {MAX_DM_CHARS}."
        ok = transition(conn, outreach_id, "drafted", "approved", approved_at=iso())
        return None if ok else f"This message is already {row['state']}."


def human_move(account_id: int, outreach_id: int, frm: str, to: str, **fields) -> bool:
    with db() as conn:
        owned = conn.execute("SELECT 1 FROM outreach WHERE id=? AND account_id=?", (outreach_id, account_id)).fetchone()
        return bool(owned) and transition(conn, outreach_id, frm, to, **fields)


# ---------------------------------------------------------------- sending

def recover_stale_sending() -> int:
    """A process that died mid-send leaves rows in 'sending'. We cannot know whether X got them."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE outreach SET state='needs_check', updated_at=?,"
            " reason='The app stopped while this was being sent. Check the DM thread on X before deciding.'"
            " WHERE state='sending' AND sending_at<?",
            (iso(), seconds_ago(get_settings().stale_sending_seconds)),
        )
        return cur.rowcount


def claim_next(account_id: int) -> int | None:
    """Atomically move this account's oldest approved row to 'sending', if every gate allows a send now."""
    s = get_settings()
    with db() as conn, tx(conn):
        acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if acc is None or acc["paused"] or acc["needs_reauth"]:
            return None
        backoff = parse_iso(acc["backoff_until"])
        if backoff and backoff > now():
            return None
        if quota.budget_locked(conn):
            return None
        if conn.execute("SELECT 1 FROM outreach WHERE account_id=? AND state='sending'", (account_id,)).fetchone():
            return None  # one in flight per account
        if quota.sends_today(conn, account_id) >= s.daily_send_cap:
            return None
        last = conn.execute("SELECT MAX(sending_at) m FROM outreach WHERE account_id=?", (account_id,)).fetchone()["m"]
        # Dry-run sends nothing to X, so pacing is shortened to keep the demo flow quick.
        spacing = min(s.send_spacing_seconds, 3) if s.dry_run else s.send_spacing_seconds
        if last and (now() - parse_iso(last)).total_seconds() < spacing:
            return None
        row = conn.execute(
            "SELECT id FROM outreach WHERE account_id=? AND state='approved' ORDER BY approved_at, id LIMIT 1",
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        claimed = transition(conn, row["id"], "approved", "sending", sending_at=iso())
        return row["id"] if claimed else None


def deliver(outreach_id: int, x: XClient) -> str:
    """Send one row that is already in 'sending'. Returns the resulting state."""
    with db() as conn:
        row = conn.execute(
            "SELECT o.*, a.x_user_id AS sender_x_user_id FROM outreach o JOIN accounts a ON a.id=o.account_id"
            " WHERE o.id=?", (outreach_id,),
        ).fetchone()
    if row is None or row["state"] != "sending":
        return row["state"] if row else "missing"
    account_id, recipient = row["account_id"], row["recipient_x_user_id"]

    def finish(to: str, **fields) -> str:
        with db() as conn:
            transition(conn, outreach_id, "sending", to, **fields)
        return to

    def requeue(reason: str, backoff_seconds: float = 0, reauth: bool = False) -> str:
        # Only for answers that prove nothing was sent.
        with db() as conn:
            transition(conn, outreach_id, "sending", "approved", sending_at=None, reason=reason)
            if backoff_seconds:
                conn.execute("UPDATE accounts SET backoff_until=? WHERE id=?",
                             (iso(now() + timedelta(seconds=backoff_seconds)), account_id))
            if reauth:
                conn.execute("UPDATE accounts SET needs_reauth=1 WHERE id=?", (account_id,))
        return "approved"

    def remember_contact() -> None:
        # Kept even if the account later deletes its data, so a reconnect cannot message this person again.
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO contact_tombstones (pair_hash, created_at) VALUES (?,?)",
                         (pair_hash(row["sender_x_user_id"], recipient), iso()))

    # 1. Has this account already talked to this person (e.g. a manual DM)? Then do not send.
    try:
        history = x.conversation_events(recipient)
    except XForbidden:
        history = []  # cannot read a thread that does not exist / is not visible
    except XRateLimited as e:
        return requeue("X rate limit while checking DM history; will retry.", backoff_seconds=e.retry_after)
    except XAuthError:
        return requeue("X sign-in expired. Reconnect the account.", reauth=True)
    except (XError, XAmbiguous) as e:
        # Nothing has been sent yet, so retrying the check is safe, a bounded number of times.
        with db() as conn:
            conn.execute("UPDATE outreach SET attempts=attempts+1 WHERE id=?", (outreach_id,))
        if row["attempts"] + 1 >= MAX_PRECHECK_ATTEMPTS:
            return finish("needs_check", reason=f"Could not check existing DM history ({e}). Nothing was sent.")
        return requeue(f"Could not check existing DM history ({e}); will retry.", backoff_seconds=60)
    if history:
        return finish("blocked", reason="There is already a DM thread with this person on this account. "
                                        "Not sent, to avoid messaging them twice.")

    # 2. The send itself. From here on, an unclear answer is never retried automatically.
    try:
        result = x.send_dm(recipient, row["text"])
    except XForbidden as e:
        return finish("blocked", reason=f"X refused the DM: {e} Usually the recipient does not follow this account "
                                        "and does not accept message requests. Not retried.")
    except XRateLimited as e:
        return requeue("X rate limit reached; will retry after the limit resets.", backoff_seconds=e.retry_after)
    except XAuthError:
        return requeue("X sign-in expired. Reconnect the account.", reauth=True)
    except XError as e:
        return finish("blocked", reason=f"X rejected the DM: {e}")
    except XAmbiguous as e:
        remember_contact()
        return finish("needs_check", reason=f"No clear answer from X ({e}). The DM may or may not have been "
                                             "delivered. Check the thread on X, then choose below. Not retried.")
    except Exception as e:  # a bug after the request went out must not lead to a resend
        remember_contact()
        return finish("needs_check", reason=f"Unexpected error during send ({type(e).__name__}: {e}). "
                                             "Check the thread on X, then choose below. Not retried.")

    quota.record(account_id, "x_dm_send", 1)
    remember_contact()
    return finish("sent", sent_at=iso(), dm_event_id=result.event_id,
                  dm_conversation_id=result.conversation_id, reason="")


def send_tick(account_id: int, x: XClient) -> str | None:
    outreach_id = claim_next(account_id)
    return deliver(outreach_id, x) if outreach_id else None
