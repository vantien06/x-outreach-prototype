import threading
import time

import pytest

from app.config import get_settings
from app.db import db, init_db, iso
from app.x.base import DMEvent, SendResult


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("SEND_SPACING_SECONDS", "0")
    monkeypatch.setenv("DAILY_SEND_CAP", "50")
    monkeypatch.setenv("GLOBAL_BUDGET_USD", "0")
    monkeypatch.setenv("WORKER_ENABLED", "false")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    init_db()
    # Build the app once under these default settings; its session cookie options are fixed at import,
    # so a test that switches to hosted mode must not be the first to import it.
    import app.main  # noqa: F401
    yield
    get_settings.cache_clear()


class FakeX:
    """Counts real send attempts. `fail` is an exception instance to raise from send_dm."""

    def __init__(self, fail=None, history=None, send_delay=0.0, me_id="7001"):
        self.fail = fail
        self.history = history or {}
        self.send_delay = send_delay
        self.me_id = me_id
        self.sent: list[tuple[str, str]] = []
        self.inbox: list[DMEvent] = []
        self.chat: dict[str, list] = {}      # conversation id -> ChatEvents returned by chat_events
        self.chat_calls: list[str] = []
        self.chat_fail = None
        self._lock = threading.Lock()

    def conversation_events(self, participant_id):
        return self.history.get(participant_id, [])

    def send_dm(self, participant_id, text):
        with self._lock:
            self.sent.append((participant_id, text))
            n = len(self.sent)
        if self.send_delay:
            time.sleep(self.send_delay)
        if self.fail:
            raise self.fail
        return SendResult(event_id=f"ev{n}", conversation_id=f"{participant_id}-{self.me_id}")

    def dm_events(self, max_results=100):
        return list(self.inbox)

    def chat_events(self, conversation_id):
        self.chat_calls.append(conversation_id)
        if self.chat_fail:
            raise self.chat_fail
        return list(self.chat.get(conversation_id, []))

    def user_posts(self, user, max_results):
        return []


def make_account(x_user_id="7001", handle="demo_sender") -> dict:
    with db() as conn:
        cur = conn.execute("INSERT INTO accounts (x_user_id, handle, created_at) VALUES (?,?,?)",
                           (x_user_id, handle, iso()))
        return dict(conn.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone())


def make_candidate(account_id: int, x_user_id: str, handle: str = "") -> int:
    with db() as conn:
        camp = conn.execute("SELECT id FROM campaigns WHERE account_id=?", (account_id,)).fetchone()
        campaign_id = camp["id"] if camp else conn.execute(
            "INSERT INTO campaigns (account_id, name, mode, pitch, status, created_at) VALUES (?,?,?,?,?,?)",
            (account_id, "t", "segment", "We back builders.", "ready", iso())).lastrowid
        cid = conn.execute(
            "INSERT INTO candidates (account_id, campaign_id, x_user_id, handle, name, bio, fit_score, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (account_id, campaign_id, x_user_id, handle or f"user{x_user_id}", "Test Person",
             "Builds robots.", 90, iso())).lastrowid
        conn.execute("INSERT INTO evidence (account_id, candidate_id, kind, ref_id, url, text) VALUES (?,?,?,?,?,?)",
                     (account_id, cid, "bio", "bio", "https://x.com/u", "Builds robots."))
        conn.execute("INSERT INTO evidence (account_id, candidate_id, kind, ref_id, url, text) VALUES (?,?,?,?,?,?)",
                     (account_id, cid, "post", "p1", "https://x.com/u/status/1", "We shipped a new gripper today."))
        return cid


def make_outreach(account: dict, x_user_id: str, state: str = "approved") -> int:
    """Draft via the real path (mock LLM), then move to the requested state."""
    from app.llm.mock import MockLLM
    from app.services import outreach

    cid = make_candidate(account["id"], x_user_id)
    oid = outreach.create_draft(account, cid, MockLLM(), FakeX())
    if state != "drafted":
        assert outreach.approve(account["id"], oid) is None
    return oid


def state_of(oid: int) -> str:
    with db() as conn:
        return conn.execute("SELECT state FROM outreach WHERE id=?", (oid,)).fetchone()["state"]
