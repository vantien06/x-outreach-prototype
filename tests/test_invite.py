"""Sign-in gate in hosted mode: allowlist, invite code, remembered invitees, and revocation."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import db, init_db
from app.services import accounts
from app.x.base import XUser

CODE = "open-sesame-42"


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("APP_MODE", "hosted")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "kq3mZ0Xv4o2m3o9Yk8m1n5p7r9t1v3x5z7B9D1F3H5J=")
    monkeypatch.setenv("BASE_URL", "https://example.test")
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    monkeypatch.setenv("X_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ALLOWED_HANDLES", "owner")
    monkeypatch.setenv("INVITE_CODE", CODE)
    get_settings.cache_clear()
    init_db()
    yield monkeypatch
    get_settings.cache_clear()


def test_allowlisted_handle_needs_no_code(hosted):
    accounts.check_allowed("Owner", "1")


def test_stranger_without_code_is_refused_and_told_about_the_invite(hosted):
    with pytest.raises(accounts.NotAllowed) as e:
        accounts.check_allowed("stranger", "2")
    assert "invite-only" in str(e.value)


def test_wrong_code_is_refused(hosted):
    with pytest.raises(accounts.NotAllowed) as e:
        accounts.check_allowed("stranger", "2", invite="nope")
    assert "not valid" in str(e.value)
    assert not accounts.is_allowed("stranger", "2")


def test_right_code_admits_and_is_remembered(hosted):
    accounts.check_allowed("stranger", "2", invite=f"  {CODE} ")     # stray spaces are forgiven
    assert accounts.is_allowed("stranger", "2")
    accounts.check_allowed("stranger", "2")                           # next sign-in: no code needed
    assert not accounts.is_allowed("someone_else", "3")               # the invite is per account


def test_changing_the_code_revokes_earlier_invitees(hosted):
    accounts.check_allowed("stranger", "2", invite=CODE)
    hosted.setenv("INVITE_CODE", "a-new-code")
    get_settings.cache_clear()
    assert not accounts.is_allowed("stranger", "2")
    with pytest.raises(accounts.NotAllowed):
        accounts.check_allowed("stranger", "2", invite=CODE)          # the old code no longer works
    accounts.check_allowed("stranger", "2", invite="a-new-code")
    assert accounts.is_allowed("stranger", "2")


def test_no_code_configured_means_allowlist_only(hosted):
    hosted.setenv("INVITE_CODE", "")
    get_settings.cache_clear()
    with pytest.raises(accounts.NotAllowed) as e:
        accounts.check_allowed("stranger", "2", invite="")
    assert "allowlist" in str(e.value)
    with pytest.raises(accounts.NotAllowed):
        accounts.check_allowed("stranger", "2", invite="anything")


# ------------------------------------------------------------------ through the HTTP sign-in flow

@pytest.fixture
def web(hosted):
    """Hosted app with the two X calls of the callback replaced, so sign-in runs end to end."""
    from app.x import real

    who = {"user": XUser(id="2", handle="stranger", name="Stranger")}
    revoked = []
    hosted.setattr(real, "exchange_code", lambda *a, **k: {"access_token": "at", "refresh_token": "rt", "expires_in": 7200})
    hosted.setattr(real.RealX, "me", lambda self: who["user"])
    hosted.setattr(real, "revoke", lambda *a, **k: revoked.append(a[-1]))
    from app.main import app

    with TestClient(app, base_url="https://example.test") as c:
        yield c, who, revoked


def sign_in(c, invite=None):
    r = c.post("/auth/login", data={} if invite is None else {"invite": invite}, follow_redirects=False)
    assert r.status_code == 303 and "x.com/i/oauth2/authorize" in r.headers["location"]
    assert "invite" not in r.headers["location"].lower() and CODE not in r.headers["location"]
    with db() as conn:
        state = conn.execute("SELECT state FROM oauth_states ORDER BY created_at DESC LIMIT 1").fetchone()["state"]
    c.get("/auth/callback", params={"code": "c", "state": state}, follow_redirects=False)
    return c.get("/")


def signed_in(page) -> bool:
    return "Stop sending" in page.text or "Sending on" in page.text


def test_http_stranger_without_code_is_turned_away_and_token_revoked(web):
    c, _, revoked = web
    page = sign_in(c)
    assert not signed_in(page) and "invite-only" in page.text
    assert revoked == ["at"]
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_http_wrong_code_then_right_code_then_remembered(web):
    c, _, _ = web
    page = sign_in(c, invite="wrong")
    assert not signed_in(page) and "not valid" in page.text
    page = sign_in(c, invite=CODE)
    assert signed_in(page)
    c.post("/auth/logout")
    page = sign_in(c)                                   # no code this time
    assert signed_in(page)


def test_http_code_in_a_link_is_ignored(web):
    c, _, _ = web
    r = c.get("/auth/login", params={"invite": CODE}, follow_redirects=False)
    assert r.status_code == 303
    with db() as conn:
        row = conn.execute("SELECT invite, state FROM oauth_states ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row["invite"] == ""
    c.get("/auth/callback", params={"code": "c", "state": row["state"]}, follow_redirects=False)
    assert not signed_in(c.get("/"))


def test_http_signed_in_invitee_is_signed_out_when_the_code_changes(web, hosted):
    c, _, _ = web
    assert signed_in(sign_in(c, invite=CODE))
    hosted.setenv("INVITE_CODE", "rotated")
    get_settings.cache_clear()
    assert not signed_in(c.get("/"))


def test_http_allowlisted_owner_signs_in_without_code(web):
    c, who, _ = web
    who["user"] = XUser(id="1", handle="Owner", name="Owner")
    assert signed_in(sign_in(c))


def test_http_invite_field_only_shown_when_a_code_is_configured(web, hosted):
    c, _, _ = web
    assert 'name="invite"' in c.get("/").text
    hosted.setenv("INVITE_CODE", "")
    get_settings.cache_clear()
    assert 'name="invite"' not in c.get("/").text
