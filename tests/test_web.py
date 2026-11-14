"""HTTP-level checks for the review queue polling and the dashboard conversation view (dry-run)."""
import pytest
from fastapi.testclient import TestClient

from app.db import db, iso

from .conftest import make_candidate, make_outreach


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        c.get("/auth/login")           # dry-run sign-in as the demo sender
        yield c


def account():
    with db() as conn:
        return dict(conn.execute("SELECT * FROM accounts ORDER BY id DESC LIMIT 1").fetchone())


def test_review_page_polls_while_drafting_and_appends_only_new_cards(client):
    acc = account()
    first = make_outreach(acc, "501", state="drafted")
    waiting = make_candidate(acc["id"], "502")
    with db() as conn:
        conn.execute("UPDATE candidates SET status='drafting' WHERE id=?", (waiting,))

    page = client.get("/review").text
    assert 'hx-get="/partials/review"' in page and "Writing 1 draft" in page
    assert f'data-outreach-id="{first}"' in page

    poll = client.get("/partials/review", params={"have": str(first)}).text
    assert "review-card" not in poll                         # nothing new: the open card is not re-sent

    second = make_outreach(acc, "503", state="drafted")
    with db() as conn:
        conn.execute("UPDATE candidates SET status='drafted' WHERE id=?", (waiting,))
    poll = client.get("/partials/review", params={"have": str(first)}).text
    assert f'data-outreach-id="{second}"' in poll and f'data-outreach-id="{first}"' not in poll
    assert 'hx-swap-oob="beforeend"' in poll
    assert 'hx-trigger' not in poll                          # drafting finished: polling stops


def test_draft_request_marks_people_as_drafting_before_redirect(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_draft_many", lambda account, ids: None)   # keep them "in progress"
    acc = account()
    cid = make_candidate(acc["id"], "601")
    with db() as conn:
        campaign_id = conn.execute("SELECT campaign_id FROM candidates WHERE id=?", (cid,)).fetchone()[0]
    r = client.post(f"/campaigns/{campaign_id}/draft", data={"candidate_id": [str(cid)]}, follow_redirects=True)
    assert "Writing 1 draft" in r.text and 'hx-get="/partials/review"' in r.text
    with db() as conn:
        assert conn.execute("SELECT status FROM candidates WHERE id=?", (cid,)).fetchone()[0] == "drafting"


def test_undrafted_count_ignores_people_who_cannot_be_messaged(client):
    acc = account()
    closed = make_candidate(acc["id"], "701")
    with db() as conn:
        conn.execute("UPDATE candidates SET can_dm=0 WHERE id=?", (closed,))
    assert "no draft yet" not in client.get("/review").text
    make_candidate(acc["id"], "702")
    assert "1 good-fit person you can message has no draft yet" in client.get("/review").text


def _sent(acc, x_user_id: str, events: list[tuple[str, str, int]]) -> int:
    oid = make_outreach(acc, x_user_id, state="drafted")
    with db() as conn:
        conn.execute("UPDATE outreach SET state='sent', sent_at=?, text=? WHERE id=?",
                     (iso(), f"Hello {x_user_id}, your gripper post stood out.", oid))
        for n, (label, text, handled) in enumerate(events):
            conn.execute(
                "INSERT INTO dm_events (account_id, event_id, sender_x_user_id, direction, text, event_at,"
                " outreach_id, label, summary, handled, created_at) VALUES (?,?,?, 'in', ?,?,?,?,?,?,?)",
                (acc["id"], f"ev-{x_user_id}-{n}", x_user_id, text, iso(), oid, label,
                 "Open to talking." if label == "interested" else "", handled, iso()))
    return oid


def test_dashboard_shows_each_kind_of_reply_in_the_thread(client):
    acc = account()
    _sent(acc, "801", [("interested", "Happy to chat, what do you look for?", 0)])
    _sent(acc, "802", [("activity", "", 1), ("encrypted", "", 0)])
    _sent(acc, "803", [("activity", "", 1)])
    _sent(acc, "804", [])
    html = client.get("/partials/dashboard").text

    assert "Happy to chat, what do you look for?" in html and "Open to talking." in html
    assert "Encrypted reply" in html and "Read on X" in html
    assert "most likely by accepting your message request" in html
    assert "No reply yet. If they do not follow you" in html
    assert "<table" not in html
    # collapsed rows keep the excerpt in one element; the full text appears once per place it is shown
    assert html.count('class="sent-excerpt"') == 4
    assert html.count("Hello 804, your gripper post stood out.") == 2      # excerpt + thread bubble
    assert html.count('data-key="sent-') == 4
    assert 'data-ts="' in html
