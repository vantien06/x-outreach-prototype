"""Reply tracking over the X Chat history: envelope classification, dedupe, and read limits."""
import base64
import struct

from app.config import get_settings
from app.db import db, iso
from app.llm.mock import MockLLM
from app.services import outreach, replies
from app.x import chat_events
from app.x.base import ChatEvent, XAmbiguous, chat_conversation_id
from tests.conftest import FakeX, make_account, make_outreach, state_of


class NoLLM:
    """Fails the test if the LLM is asked to read something it must not see."""

    def classify_reply(self, our_message, reply):
        raise AssertionError("LLM must not be called for this event")


# ---------------------------------------------------------------- tiny Thrift writer for fixtures

def t_string(fid: int, value: bytes) -> bytes:
    return b"\x0b" + struct.pack(">h", fid) + struct.pack(">i", len(value)) + value


def t_bool(fid: int, value: bool) -> bytes:
    return b"\x02" + struct.pack(">h", fid) + (b"\x01" if value else b"\x00")


def t_struct(fid: int, body: bytes) -> bytes:
    return b"\x0c" + struct.pack(">h", fid) + body + b"\x00"


def envelope(union_member: int, inner: bytes) -> str:
    raw = t_string(1, b"111") + t_string(3, b"100") + t_struct(7, t_struct(union_member, inner)) + b"\x00"
    return base64.b64encode(raw).decode().rstrip("=")


def api_event(encoded: str, event_id="e1", sender="100") -> dict:
    return {"id": event_id, "sender_id": sender, "created_at": "2026-11-14T13:33:29.000Z",
            "conversation_id": "100:7001", "encoded_event": encoded}


PLAIN_BODY = t_struct(1, t_struct(1, t_string(1, "Sounds good, tell me more".encode()))) + b"\x00"
CIPHERTEXT = bytes(range(7, 95))  # 88 opaque bytes, like the live sample


def test_classify_plain_message_exposes_text():
    ev = chat_events.classify(api_event(envelope(1, t_string(100, PLAIN_BODY) + t_bool(102, True))))
    assert (ev.kind, ev.text) == ("message", "Sounds good, tell me more")
    assert ev.conversation_id == "100-7001"


def test_classify_encrypted_message_has_no_text():
    ev = chat_events.classify(api_event(envelope(1, t_string(100, CIPHERTEXT) + t_bool(102, True))))
    assert (ev.kind, ev.text) == ("encrypted", "")


def test_classify_silent_encrypted_event_is_activity_not_a_reply():
    ev = chat_events.classify(api_event(envelope(1, t_string(100, CIPHERTEXT[:49]) + t_bool(102, False))))
    assert ev.kind == "activity"
    # 102 missing altogether is treated the same, conservatively.
    assert chat_events.classify(api_event(envelope(1, t_string(100, CIPHERTEXT)))).kind == "activity"


def test_classify_key_change_is_system():
    assert chat_events.classify(api_event(envelope(3, t_string(1, b"1794663206468")))).kind == "system"


def test_classify_garbage_is_unknown_and_never_raises():
    for encoded in ("", "not base64 !!", base64.b64encode(b"\x0b\x00\x01\x7f\xff\xff\xff").decode(),
                    base64.b64encode(b"\x00").decode()):
        assert chat_events.classify(api_event(encoded)).kind == "unknown"
    assert chat_events.classify({}).kind == "unknown"


def test_conversation_id_puts_smaller_user_id_first():
    assert chat_conversation_id("1979887450412695552", "1850962672403320832") == \
        "1850962672403320832-1979887450412695552"
    assert chat_conversation_id("99", "1000") == "99-1000"  # numeric order, not text order


# ---------------------------------------------------------------- recording

def sent_row(acc, recipient="100"):
    oid = make_outreach(acc, recipient)
    x = FakeX()
    assert outreach.send_tick(acc["id"], x) == "sent"
    return oid, x


def make_due(oid=None):
    with db() as conn:
        conn.execute("UPDATE outreach SET last_reply_check_at=NULL" + (" WHERE id=?" if oid else ""),
                     (oid,) if oid else ())


def inbound(conn):
    return conn.execute("SELECT * FROM dm_events WHERE direction='in' ORDER BY id").fetchall()


def test_encrypted_reply_recorded_once_without_llm():
    acc = make_account()
    oid, x = sent_row(acc)
    x.chat["100-7001"] = [
        ChatEvent(id="k1", conversation_id="100-7001", sender_id="100", created_at=iso(), kind="system"),
        ChatEvent(id="a1", conversation_id="100-7001", sender_id="100", created_at=iso(), kind="activity"),
        ChatEvent(id="r1", conversation_id="100-7001", sender_id="100", created_at=iso(), kind="encrypted"),
    ]
    assert replies.poll(acc, x, NoLLM()) == 1
    make_due()
    assert replies.poll(acc, x, NoLLM()) == 0
    with db() as conn:
        rows = inbound(conn)
        by_label = {r["label"]: r for r in rows}
        assert set(by_label) == {"encrypted", "activity"}          # the key change is not stored at all
        assert by_label["encrypted"]["text"] == "" and by_label["encrypted"]["handled"] == 0
        assert by_label["encrypted"]["outreach_id"] == oid
        assert by_label["activity"]["handled"] == 1                # shown as status, not as work to do


def test_activity_alone_is_not_counted_as_a_reply():
    from app.main import dashboard_data

    acc = make_account()
    _, x = sent_row(acc)
    x.chat["100-7001"] = [
        ChatEvent(id="a1", conversation_id="100-7001", sender_id="100", created_at=iso(), kind="activity")]
    assert replies.poll(acc, x, NoLLM()) == 0
    d = dashboard_data(acc)
    assert d["replied"] == 0 and d["attention"] == 0
    assert d["sent"][0]["reply_label"] == "activity"


def test_unknown_event_from_recipient_needs_attention_but_is_not_called_a_reply():
    from app.main import dashboard_data

    acc = make_account()
    _, x = sent_row(acc)
    x.chat["100-7001"] = [
        ChatEvent(id="u1", conversation_id="100-7001", sender_id="100", created_at=iso(), kind="unknown")]
    assert replies.poll(acc, x, NoLLM()) == 0
    d = dashboard_data(acc)
    assert d["replied"] == 0 and d["attention"] == 1 and d["replies_open"][0]["label"] == "unknown"


def test_readable_reply_is_labelled_by_llm_and_preferred_over_activity():
    from app.main import dashboard_data

    acc = make_account()
    _, x = sent_row(acc)
    x.chat["100-7001"] = [
        ChatEvent(id="r1", conversation_id="100-7001", sender_id="100", created_at=iso(), kind="message",
                  text="Please don't contact me again."),
        ChatEvent(id="a2", conversation_id="100-7001", sender_id="100", created_at=iso(), kind="activity"),
    ]
    assert replies.poll(acc, x, MockLLM()) == 1
    d = dashboard_data(acc)
    assert d["replied"] == 1 and d["sent"][0]["reply_label"] == "opt_out"


def test_events_from_others_or_before_our_message_are_ignored():
    acc = make_account()
    _, x = sent_row(acc)
    x.chat["100-7001"] = [
        ChatEvent(id="old", conversation_id="100-7001", sender_id="100", created_at="2020-01-01T00:00:00.000Z",
                  kind="encrypted"),
        ChatEvent(id="other", conversation_id="100-7001", sender_id="555", created_at=iso(), kind="encrypted"),
        ChatEvent(id="mine", conversation_id="100-7001", sender_id="7001", created_at=iso(), kind="message",
                  text="our own message"),
    ]
    assert replies.poll(acc, x, NoLLM()) == 0
    with db() as conn:
        assert inbound(conn) == []


def test_needs_check_confirmed_when_recipient_replies():
    acc = make_account()
    oid = make_outreach(acc, "100")
    outreach.send_tick(acc["id"], FakeX(fail=XAmbiguous("timeout")))
    assert state_of(oid) == "needs_check"
    x = FakeX()
    # No conversation id was returned by the failed send, so it is derived from the two user ids.
    x.chat["100-7001"] = [
        ChatEvent(id="r1", conversation_id="100-7001", sender_id="100", created_at=iso(), kind="encrypted")]
    assert replies.poll(acc, x, NoLLM()) == 1
    assert state_of(oid) == "sent"
    assert x.chat_calls == ["100-7001"]


# ---------------------------------------------------------------- rotation and limits

def many_sent(acc, n):
    ids = []
    for i in range(n):
        oid = make_outreach(acc, str(200 + i))
        assert outreach.send_tick(acc["id"], FakeX()) == "sent"
        ids.append(oid)
    return ids


def test_rotation_stays_under_read_limits(monkeypatch):
    monkeypatch.setenv("CHAT_READS_PER_TICK", "8")
    monkeypatch.setenv("CHAT_READS_PER_15MIN", "20")
    get_settings.cache_clear()
    acc = make_account()
    many_sent(acc, 30)
    x = FakeX()
    for expected_total in (8, 16, 20, 20, 20):
        make_due()                      # everything is due again; only the limit holds it back
        replies.poll(acc, x, NoLLM())
        assert len(x.chat_calls) == expected_total


def test_daily_read_limit_is_respected(monkeypatch):
    monkeypatch.setenv("CHAT_READS_PER_DAY", "5")
    get_settings.cache_clear()
    acc = make_account()
    many_sent(acc, 12)
    x = FakeX()
    for _ in range(3):
        make_due()
        replies.poll(acc, x, NoLLM())
    assert len(x.chat_calls) == 5


def test_rotation_covers_every_conversation_before_repeating(monkeypatch):
    monkeypatch.setenv("CHAT_READS_PER_TICK", "4")
    get_settings.cache_clear()
    acc = make_account()
    many_sent(acc, 10)
    x = FakeX()
    replies.poll(acc, x, NoLLM())
    replies.poll(acc, x, NoLLM())
    replies.poll(acc, x, NoLLM())
    assert len(x.chat_calls) == 10 and len(set(x.chat_calls)) == 10
    # Never-checked conversations go newest send first.
    assert x.chat_calls[0] == "209-7001"


def test_not_due_conversations_cost_no_reads():
    acc = make_account()
    many_sent(acc, 3)
    x = FakeX()
    replies.poll(acc, x, NoLLM())
    replies.poll(acc, x, NoLLM())       # immediately again: nothing is due yet
    assert len(x.chat_calls) == 3


def test_reloading_the_dashboard_asks_for_a_fresh_check_once():
    acc = make_account()
    many_sent(acc, 3)
    x = FakeX()
    replies.poll(acc, x, NoLLM())
    assert replies.request_check(acc["id"]) == 0          # checked a moment ago: nothing to do
    with db() as conn:
        conn.execute("UPDATE outreach SET last_reply_check_at='2026-01-01T00:00:00.000000Z'")
    assert replies.request_check(acc["id"]) == 3
    assert replies.request_check(acc["id"]) == 0          # already due: reloading again adds nothing
    replies.poll(acc, x, NoLLM())
    assert len(x.chat_calls) == 6


def test_check_interval_backs_off_with_age(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    hour, day = 3600, 86400
    steps = [replies.check_interval_seconds(a) for a in (60, 20 * 60, 2 * hour, 2 * day, 30 * day)]
    assert steps == sorted(steps) and steps[:2] == [30, 120] and steps[-1] == 6 * hour


def test_failed_read_still_uses_allowance_and_reports(monkeypatch):
    from app.x.base import XRateLimited
    import pytest

    acc = make_account()
    many_sent(acc, 2)
    x = FakeX()
    x.chat_fail = XRateLimited("slow down")
    with pytest.raises(XRateLimited):
        replies.poll(acc, x, NoLLM())
    with db() as conn:
        assert conn.execute("SELECT COALESCE(SUM(units),0) FROM usage_ledger WHERE kind='x_chat_read'").fetchone()[0] == 1
