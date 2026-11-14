"""The property that must never break: one recipient gets at most one message per account."""
import sqlite3
import threading

import pytest

from app.db import db, iso
from app.llm.mock import MockLLM
from app.services import accounts, outreach, replies
from app.x.base import ChatEvent, DMEvent, XAmbiguous, XError, XForbidden, XRateLimited

from .conftest import FakeX, make_account, make_candidate, make_outreach, state_of


def test_drafting_twice_returns_the_same_row():
    acc = make_account()
    cid = make_candidate(acc["id"], "100")
    a = outreach.create_draft(acc, cid, MockLLM(), FakeX())
    b = outreach.create_draft(acc, cid, MockLLM(), FakeX())
    assert a == b


def test_database_refuses_second_row_for_same_recipient():
    acc = make_account()
    oid = make_outreach(acc, "100")
    with db() as conn:
        row = conn.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO outreach (account_id, campaign_id, candidate_id, recipient_x_user_id, recipient_handle,"
                " state, created_at, updated_at) VALUES (?,?,?,?,?, 'approved', ?, ?)",
                (acc["id"], row["campaign_id"], row["candidate_id"], "100", "x", iso(), iso()))


def test_concurrent_workers_send_once():
    acc = make_account()
    oid = make_outreach(acc, "100")
    x = FakeX(send_delay=0.2)
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        outreach.send_tick(acc["id"], x)

    threads = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(x.sent) == 1
    assert state_of(oid) == "sent"


def test_timeout_parks_in_needs_check_and_is_never_resent():
    acc = make_account()
    oid = make_outreach(acc, "100")
    x = FakeX(fail=XAmbiguous("read timeout"))
    assert outreach.send_tick(acc["id"], x) == "needs_check"
    for _ in range(5):
        assert outreach.send_tick(acc["id"], x) is None
    assert len(x.sent) == 1
    assert state_of(oid) == "needs_check"


def test_unexpected_exception_during_send_is_not_resent():
    acc = make_account()
    oid = make_outreach(acc, "100")
    x = FakeX(fail=RuntimeError("bug"))
    assert outreach.send_tick(acc["id"], x) == "needs_check"
    assert outreach.send_tick(acc["id"], x) is None
    assert len(x.sent) == 1


def test_403_is_blocked_with_reason_and_not_retried():
    acc = make_account()
    oid = make_outreach(acc, "100")
    x = FakeX(fail=XForbidden("You cannot send messages to this user."))
    assert outreach.send_tick(acc["id"], x) == "blocked"
    assert outreach.send_tick(acc["id"], x) is None
    assert len(x.sent) == 1
    with db() as conn:
        assert "refused" in conn.execute("SELECT reason FROM outreach WHERE id=?", (oid,)).fetchone()["reason"]


def test_other_4xx_is_blocked():
    acc = make_account()
    oid = make_outreach(acc, "100")
    assert outreach.send_tick(acc["id"], FakeX(fail=XError("400: bad request"))) == "blocked"


def test_rate_limit_requeues_and_backs_off():
    acc = make_account()
    oid = make_outreach(acc, "100")
    x = FakeX(fail=XRateLimited("too many", retry_after=600))
    assert outreach.send_tick(acc["id"], x) == "approved"
    assert state_of(oid) == "approved"
    # Backoff holds the queue: no second attempt now.
    assert outreach.send_tick(acc["id"], x) is None
    assert len(x.sent) == 1


def test_existing_dm_thread_blocks_send():
    acc = make_account()
    oid = make_outreach(acc, "100")
    x = FakeX(history={"100": [DMEvent(id="old", conversation_id="100-7001", sender_id="7001", text="hi")]})
    assert outreach.send_tick(acc["id"], x) == "blocked"
    assert x.sent == []


def test_stale_sending_row_goes_to_needs_check_not_back_to_queue(monkeypatch):
    acc = make_account()
    oid = make_outreach(acc, "100")
    assert outreach.claim_next(acc["id"]) == oid  # simulate a crash right after the claim
    with db() as conn:
        conn.execute("UPDATE outreach SET sending_at='2020-01-01T00:00:00.000000Z' WHERE id=?", (oid,))
    assert outreach.recover_stale_sending() == 1
    assert state_of(oid) == "needs_check"
    x = FakeX()
    assert outreach.send_tick(acc["id"], x) is None
    assert x.sent == []


def test_needs_check_resolved_only_by_human_or_history():
    acc = make_account()
    oid = make_outreach(acc, "100")
    outreach.send_tick(acc["id"], FakeX(fail=XAmbiguous("timeout")))
    assert outreach.human_move(acc["id"], oid, "needs_check", "sent", sent_at=iso())
    assert state_of(oid) == "sent"


def test_needs_check_confirmed_by_dm_history():
    acc = make_account()
    oid = make_outreach(acc, "100")
    outreach.send_tick(acc["id"], FakeX(fail=XAmbiguous("timeout")))
    with db() as conn:
        text = conn.execute("SELECT text FROM outreach WHERE id=?", (oid,)).fetchone()["text"]
    x = FakeX()
    x.chat["100-7001"] = [ChatEvent(id="e1", conversation_id="100-7001", sender_id="7001", text=text,
                                    created_at=iso(), kind="message")]
    replies.poll(acc, x, MockLLM())
    assert state_of(oid) == "sent"


def test_reconnect_after_data_deletion_cannot_message_again():
    acc = make_account()
    make_outreach(acc, "100")
    assert outreach.send_tick(acc["id"], FakeX()) == "sent"
    accounts.disconnect_and_delete(acc["id"])
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0] == 0

    acc2 = make_account()  # same X user signs in again
    oid = make_outreach(acc2, "100", state="drafted")
    assert state_of(oid) == "blocked"
    x = FakeX()
    assert outreach.send_tick(acc2["id"], x) is None
    assert x.sent == []


def test_two_accounts_are_independent():
    a = make_account("7001", "a")
    b = make_account("7002", "b")
    make_outreach(a, "100")
    make_outreach(b, "100")
    xa, xb = FakeX(me_id="7001"), FakeX(me_id="7002")
    assert outreach.send_tick(a["id"], xa) == "sent"
    assert outreach.send_tick(b["id"], xb) == "sent"
    assert len(xa.sent) == len(xb.sent) == 1
