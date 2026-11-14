import pytest

from app.config import get_settings
from app.db import db, iso
from app.llm.mock import MockLLM
from app.services import accounts, discovery, outreach, quota, replies
from app.x.base import ChatEvent, DMEvent
from app.x.mock import MockX

from .conftest import FakeX, make_account, make_candidate, make_outreach, state_of


# ---------------------------------------------------------------- state machine

@pytest.mark.parametrize("frm,to", [
    ("drafted", "sent"), ("drafted", "sending"), ("sent", "approved"), ("sent", "sending"),
    ("blocked", "approved"), ("blocked", "sending"), ("needs_check", "sending"), ("skipped", "approved"),
])
def test_illegal_transitions_raise(frm, to):
    acc = make_account()
    oid = make_outreach(acc, "100", state="drafted")
    with db() as conn:
        with pytest.raises(outreach.InvalidTransition):
            outreach.transition(conn, oid, frm, to)


def test_transition_is_conditional_on_current_state():
    acc = make_account()
    oid = make_outreach(acc, "100", state="drafted")
    with db() as conn:
        assert outreach.transition(conn, oid, "approved", "sending") is False  # row is 'drafted'
        assert outreach.transition(conn, oid, "drafted", "approved") is True
        assert outreach.transition(conn, oid, "drafted", "approved") is False  # second time: no-op
    assert state_of(oid) == "approved"


def test_only_approved_rows_are_sent():
    acc = make_account()
    oid = make_outreach(acc, "100", state="drafted")
    x = FakeX()
    assert outreach.send_tick(acc["id"], x) is None
    assert x.sent == [] and state_of(oid) == "drafted"


def test_unapprove_before_send_and_not_after():
    acc = make_account()
    oid = make_outreach(acc, "100")
    assert outreach.human_move(acc["id"], oid, "approved", "drafted")
    assert outreach.approve(acc["id"], oid) is None
    outreach.send_tick(acc["id"], FakeX())
    assert not outreach.human_move(acc["id"], oid, "approved", "drafted")
    assert state_of(oid) == "sent"


def test_cannot_act_on_another_accounts_outreach():
    a, b = make_account("7001", "a"), make_account("7002", "b")
    oid = make_outreach(a, "100", state="drafted")
    assert outreach.approve(b["id"], oid) == "Not found."
    assert not outreach.human_move(b["id"], oid, "drafted", "skipped")
    assert state_of(oid) == "drafted"


def test_empty_message_cannot_be_approved():
    acc = make_account()
    oid = make_outreach(acc, "100", state="drafted")
    with db() as conn:
        conn.execute("UPDATE outreach SET text='' WHERE id=?", (oid,))
    assert outreach.approve(acc["id"], oid) == "The message is empty."


# ---------------------------------------------------------------- limits

def test_daily_send_cap(monkeypatch):
    monkeypatch.setenv("DAILY_SEND_CAP", "2")
    get_settings.cache_clear()
    acc = make_account()
    for i in range(5):
        make_outreach(acc, str(100 + i))
    x = FakeX()
    results = [outreach.send_tick(acc["id"], x) for _ in range(10)]
    assert results.count("sent") == 2
    assert len(x.sent) == 2


def test_spacing_between_sends(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setenv("SEND_SPACING_SECONDS", "3600")
    get_settings.cache_clear()
    acc = make_account()
    make_outreach(acc, "100")
    make_outreach(acc, "101")
    x = FakeX()
    assert outreach.send_tick(acc["id"], x) == "sent"
    assert outreach.send_tick(acc["id"], x) is None
    assert len(x.sent) == 1


def test_paused_account_sends_nothing():
    acc = make_account()
    make_outreach(acc, "100")
    accounts.set_paused(acc["id"], True)
    x = FakeX()
    assert outreach.send_tick(acc["id"], x) is None
    accounts.set_paused(acc["id"], False)
    assert outreach.send_tick(acc["id"], x) == "sent"


def test_global_budget_locks_send_draft_and_discovery(monkeypatch):
    monkeypatch.setenv("GLOBAL_BUDGET_USD", "1")
    get_settings.cache_clear()
    acc = make_account()
    make_outreach(acc, "100")
    with db() as conn:
        conn.execute("INSERT INTO usage_ledger (account_id, kind, units, est_cost_usd, created_at)"
                     " VALUES (?, 'x_post_read', 300, 1.5, '2026-01-01T00:00:00.000000Z')", (acc["id"],))
        assert quota.budget_locked(conn)
    x = FakeX()
    assert outreach.send_tick(acc["id"], x) is None and x.sent == []
    with pytest.raises(quota.LimitReached):
        outreach.create_draft(acc, make_candidate(acc["id"], "200"), MockLLM(), FakeX())


def test_daily_candidate_cap(monkeypatch):
    monkeypatch.setenv("DAILY_CANDIDATE_CAP", "3")
    get_settings.cache_clear()
    acc = make_account()
    x, llm = MockX(acc["x_user_id"]), MockLLM()

    def campaign() -> int:
        with db() as conn:
            return conn.execute(
                "INSERT INTO campaigns (account_id, name, mode, segment, pitch, created_at)"
                " VALUES (?, 'c', 'segment', 'robotics founders', 'p', '2026-01-01')", (acc["id"],)).lastrowid

    first = campaign()
    discovery.run_campaign(first, x, llm)
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM candidates WHERE fit_score IS NOT NULL").fetchone()[0] == 3
    second = campaign()
    discovery.run_campaign(second, x, llm)
    with db() as conn:
        row = conn.execute("SELECT status, note FROM campaigns WHERE id=?", (second,)).fetchone()
        assert row["status"] == "failed" and "Daily limit" in row["note"]
        assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 3


def test_hosted_allowlist(monkeypatch):
    monkeypatch.setenv("APP_MODE", "hosted")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "x" * 10)
    monkeypatch.setenv("ALLOWED_HANDLES", "@GenAIFund, tester")
    get_settings.cache_clear()
    accounts.check_allowed("genaifund")
    accounts.check_allowed("Tester")
    with pytest.raises(accounts.NotAllowed):
        accounts.check_allowed("stranger")


def test_hosted_empty_allowlist_admits_nobody(monkeypatch):
    monkeypatch.setenv("APP_MODE", "hosted")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ALLOWED_HANDLES", "")
    get_settings.cache_clear()
    with pytest.raises(accounts.NotAllowed):
        accounts.check_allowed("anyone")


# ---------------------------------------------------------------- discovery, grounding, replies

def test_discovery_keeps_evidence_and_filters_poor_fits():
    acc = make_account()
    with db() as conn:
        cid = conn.execute(
            "INSERT INTO campaigns (account_id, name, mode, segment, pitch, created_at)"
            " VALUES (?, 'c', 'segment', 'robotics and AI builders', 'p', '2026-01-01')", (acc["id"],)).lastrowid
    discovery.run_campaign(cid, MockX(acc["x_user_id"]), MockLLM())
    with db() as conn:
        assert conn.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()["status"] == "ready"
        rows = conn.execute("SELECT * FROM candidates").fetchall()
        by_handle = {r["handle"]: r for r in rows}
        assert by_handle["demo_linh_robotics"]["fit_score"] >= 60
        assert by_handle["demo_grace_foodie"]["fit_score"] < 60
        assert "demo_tom_cryptoalpha" not in by_handle   # promo account: left out before scoring
        for r in rows:
            assert conn.execute("SELECT COUNT(*) FROM evidence WHERE candidate_id=?", (r["id"],)).fetchone()[0] >= 1
            assert r["fit_reason"]


def test_claims_without_stored_evidence_are_dropped_and_flagged():
    kept, note = outreach.check_grounding(
        "Hi", [{"claim": "You posted X", "evidence_id": "E1"}, {"claim": "We met in Hanoi", "evidence_id": "E999"}],
        {"E1"})
    assert kept == [{"claim": "You posted X", "evidence_id": "E1"}]
    assert "We met in Hanoi" in note


def test_draft_cites_only_stored_evidence():
    acc = make_account()
    oid = make_outreach(acc, "100", state="drafted")
    with db() as conn:
        row = conn.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
        ids = {f"E{r['id']}" for r in conn.execute("SELECT id FROM evidence WHERE candidate_id=?", (row["candidate_id"],))}
    import json
    claims = json.loads(row["claims"])
    assert claims and all(c["evidence_id"] in ids for c in claims)
    assert "We shipped a new gripper today." in row["text"]


def test_reply_is_recorded_once_and_labelled():
    acc = make_account()
    oid = make_outreach(acc, "100")
    x = FakeX()
    outreach.send_tick(acc["id"], x)
    x.chat["100-7001"] = [ChatEvent(id="r1", conversation_id="100-7001", sender_id="100", created_at=iso(),
                                    kind="message", text="Please don't contact me again.")]
    # Another conversation on the account: never requested, so never stored.
    x.chat["555-7001"] = [ChatEvent(id="u1", conversation_id="555-7001", sender_id="555", created_at=iso(),
                                    kind="message", text="unrelated DM from a friend")]
    assert replies.poll(acc, x, MockLLM()) == 1
    with db() as conn:
        conn.execute("UPDATE outreach SET last_reply_check_at=NULL")
    assert replies.poll(acc, x, MockLLM()) == 0
    assert x.chat_calls == ["100-7001", "100-7001"]
    with db() as conn:
        rows = conn.execute("SELECT * FROM dm_events WHERE direction='in'").fetchall()
        assert len(rows) == 1 and rows[0]["label"] == "opt_out" and rows[0]["outreach_id"] == oid
        # Unrelated conversations are never stored.
        assert conn.execute("SELECT COUNT(*) FROM dm_events WHERE text LIKE '%friend%'").fetchone()[0] == 0
