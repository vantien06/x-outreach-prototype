"""Reply tracking.

Replies typed in the X app are X Chat events. They do NOT appear on the legacy
DM endpoints (/2/dm_events), only in the chat history of each conversation
(GET /2/chat/conversations/{id}/events), and their text is end-to-end encrypted.
So this module:

  * polls the chat history of each conversation we are waiting on, one request
    per conversation, in a rotation that stays under X's read limits;
  * records WHO replied and WHEN. Text is stored only when X returns it in the
    clear; an encrypted reply is recorded with label 'encrypted' and the operator
    reads it on X;
  * settles 'needs_check' rows when our own message shows up in the history.
"""
from __future__ import annotations

from datetime import timedelta

from ..config import get_settings
from ..db import db, iso, now, parse_iso
from ..llm.base import LLMClient, LLMError
from ..x.base import ChatEvent, XClient, chat_conversation_id
from . import quota
from .outreach import transition

# Labels that are NOT a reply from the recipient.
NON_REPLY_LABELS = ("activity", "unknown")

ENCRYPTED_SUMMARY = "End-to-end encrypted X Chat message. Open the conversation on X to read it."
ACTIVITY_SUMMARY = ("The recipient did something in the conversation that is not a visible message, "
                    "typically accepting your message request.")
UNKNOWN_SUMMARY = "Activity from the recipient that this tool could not identify. Open the conversation on X."


def check_interval_seconds(age_seconds: float) -> float:
    """How long to wait between background checks of one conversation, by time since it was sent.

    Kept sparse on purpose: every check is a billed X read. The fresh check happens when the
    operator reloads the dashboard (see request_check); this is only a safety net.
    """
    if get_settings().dry_run:
        return 2
    if age_seconds < 86400:
        return 3600           # first day: hourly
    if age_seconds < 7 * 86400:
        return 6 * 3600       # first week: every 6 hours
    return 24 * 3600          # after that: once a day


def conversation_id_for(row, me: str) -> str:
    return row["dm_conversation_id"] or chat_conversation_id(me, row["recipient_x_user_id"])


def read_allowance(account_id: int) -> int:
    """Chat-history requests this account may make right now."""
    s = get_settings()
    with db() as conn:
        used_15 = quota.chat_reads_since(conn, account_id, iso(now() - timedelta(minutes=15)))
        used_day = quota.chat_reads_since(conn, account_id, iso(now() - timedelta(hours=24)))
    return max(0, min(s.chat_reads_per_tick, s.chat_reads_per_15min - used_15, s.chat_reads_per_day - used_day))


def due(account_id: int, limit: int) -> list:
    """Conversations due a check: never-checked first (newest send first), then most overdue."""
    if limit <= 0:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM outreach WHERE account_id=? AND state IN ('sent','needs_check')", (account_id,)
        ).fetchall()
    t = now()
    ranked = []
    for r in rows:
        started = parse_iso(r["sent_at"] or r["sending_at"]) or t
        last = parse_iso(r["last_reply_check_at"])
        if last is None:
            ranked.append((0, -started.timestamp(), r))
            continue
        overdue = (t - last).total_seconds() / check_interval_seconds((t - started).total_seconds())
        if overdue >= 1:
            ranked.append((1, -overdue, r))
    ranked.sort(key=lambda item: item[:2])
    return [r for _, _, r in ranked[:limit]]


def request_check(account_id: int, limit: int = 8, min_gap_seconds: float = 10) -> int:
    """Make the most recently sent conversations due now, so the worker checks them on its next tick.

    Called when the operator reloads the dashboard. Conversations checked in the last few seconds are
    left alone, so reloading repeatedly does not spend extra reads. Returns how many were made due.
    """
    cutoff = iso(now() - timedelta(seconds=min_gap_seconds))
    with db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM outreach WHERE account_id=? AND state IN ('sent','needs_check')"
            " AND last_reply_check_at IS NOT NULL AND last_reply_check_at < ?"
            " ORDER BY COALESCE(sent_at, sending_at) DESC LIMIT ?", (account_id, cutoff, limit))]
        if ids:
            conn.execute(f"UPDATE outreach SET last_reply_check_at=NULL WHERE id IN ({','.join('?' * len(ids))})", ids)
            conn.execute("UPDATE accounts SET last_reply_poll_at=NULL WHERE id=?", (account_id,))
    return len(ids)


def poll(account, x: XClient, llm: LLMClient) -> int:
    """Check the conversations that are due. Returns the number of new replies stored.

    X errors propagate to the caller (the worker reports them and tries later).
    """
    account_id, me = account["id"], account["x_user_id"]
    new_replies = 0
    for row in due(account_id, read_allowance(account_id)):
        # Counted before the call so a failing endpoint still uses up allowance.
        quota.record(account_id, "x_chat_read", 1)
        with db() as conn:
            conn.execute("UPDATE outreach SET last_reply_check_at=?, reply_checks=reply_checks+1 WHERE id=?",
                         (iso(), row["id"]))
        events = x.chat_events(conversation_id_for(row, me))
        for ev in sorted(events, key=lambda e: e.created_at):
            new_replies += _record(account_id, me, row["id"], ev, llm)
    return new_replies


def _record(account_id: int, me: str, outreach_id: int, ev: ChatEvent, llm: LLMClient) -> int:
    """Store one event if it matters. Returns 1 if it is a new reply from the recipient."""
    if ev.kind == "system" or not ev.id:
        return 0
    with db() as conn:
        if conn.execute("SELECT 1 FROM dm_events WHERE account_id=? AND event_id=?", (account_id, ev.id)).fetchone():
            return 0
        row = conn.execute("SELECT * FROM outreach WHERE id=?", (outreach_id,)).fetchone()
        if ev.sender_id == me:
            # Our own message. If the send had no clear answer, this proves it was delivered.
            if row["state"] == "needs_check" and ev.kind == "message" and _after(ev.created_at, row["sending_at"]) \
                    and ev.text.strip() == row["text"].strip():
                transition(conn, row["id"], "needs_check", "sent", sent_at=ev.created_at or iso(),
                           dm_event_id=ev.id, dm_conversation_id=ev.conversation_id,
                           reason="Confirmed delivered: the message appears in the conversation history.")
            return 0
    if ev.sender_id != row["recipient_x_user_id"] or not _after(ev.created_at, row["sending_at"]):
        return 0  # someone else, or older than our message

    label, summary, text, handled = None, None, ev.text, 0
    if ev.kind == "message":
        try:
            out = llm.classify_reply(row["text"], ev.text)
            label, summary = out.label, out.summary
            quota.record(account_id, "llm_fast", 1)
        except LLMError as e:
            summary = f"Could not be labelled automatically ({e})."
    elif ev.kind == "encrypted":
        label, summary, text = "encrypted", ENCRYPTED_SUMMARY, ""
    elif ev.kind == "activity":
        label, summary, text, handled = "activity", ACTIVITY_SUMMARY, "", 1
    else:
        label, summary, text = "unknown", UNKNOWN_SUMMARY, ""

    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO dm_events (account_id, event_id, conversation_id, sender_x_user_id, direction,"
            " text, event_at, outreach_id, label, summary, handled, created_at) VALUES (?,?,?,?, 'in', ?,?,?,?,?,?,?)",
            (account_id, ev.id, ev.conversation_id, ev.sender_id, text, ev.created_at, outreach_id,
             label, summary, handled, iso()),
        )
        if cur.rowcount and row["state"] == "needs_check":
            # They are active in the thread, so the message did arrive.
            transition(conn, row["id"], "needs_check", "sent", sent_at=row["sending_at"],
                       reason="Confirmed delivered: the recipient responded in the conversation.")
        return cur.rowcount if label not in NON_REPLY_LABELS else 0


def _after(event_at: str, sending_at: str | None) -> bool:
    try:
        a, b = parse_iso(event_at), parse_iso(sending_at)
    except ValueError:
        return False
    return bool(a and b and (a - b).total_seconds() > -120)
