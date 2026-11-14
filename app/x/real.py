"""Live X API v2 client (OAuth 2.0 user context, pay-per-use).

STATUS as of 2026-11-14. "Live" = exercised against the real API with a real
account; "docs only" = written from docs.x.com and never run for real.

  Live, works as coded:
    authorize   https://x.com/i/oauth2/authorize            (PKCE, S256)
    token       POST https://api.x.com/2/oauth2/token        code exchange only
    GET  /2/users/me
    GET  /2/users/by?usernames=                              `receives_your_dm` is returned
    POST /2/dm_conversations/with/:participant_id/messages   201; headers: 99 per 15 min, 1,440 per 24 h per user.
                                                             The DM arrives in the recipient's Message requests.
    GET  /2/dm_conversations/with/:participant_id/dm_events  200 with result_count 0 when there is no thread
                                                             (not 404). 15 per 15 min. Used for the pre-send check.
    GET  /2/dm_events                                        works but lags about 15 minutes; 15 per 15 min.
                                                             No longer used by the app.
    GET  /2/chat/conversations/{idA-idB}/events              works with the same token and scopes; 100 per 15 min,
                                                             10,000 per 24 h. The only place a reply typed in the
                                                             X app shows up; its text is end-to-end encrypted.

    GET  /2/tweets/search/recent                             ran in the hosted test (first page only,
                                                             last 7 days, max_results 10..100).
    GET  /2/users/:id/following                              ran in the hosted test (needs follows.read).
    GET  /2/users/:id/tweets                                 ran in the hosted test, including for a protected
                                                             account the sender follows.

  Docs only, never run for real:
    token refresh and POST /2/oauth2/revoke
    `next_token` paging of recent search (search_page) and the `location` user field,
    both added after the hosted test.
    How X answers a refused DM (403), a rate limit (429) and a read of a protected
    account's posts: the handling follows the documented status codes, none has been seen yet.
    A 404 from the chat events endpoint is treated as "no conversation yet"; not observed.

  Not verified: pagination of chat events (only the first page is read) and the
  price of chat-history reads.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Callable
from urllib.parse import urlencode

import httpx

from . import chat_events as chat_envelope
from .base import ChatEvent, DMEvent, SendResult, XAmbiguous, XAuthError, XError, XForbidden, XPost, XRateLimited, XUser

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
API = "https://api.x.com/2"
SCOPES = ["tweet.read", "users.read", "follows.read", "dm.read", "dm.write", "offline.access"]
USER_FIELDS = "description,location,public_metrics,receives_your_dm"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def new_pkce() -> tuple[str, str, str]:
    """Returns (state, code_verifier, code_challenge)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return secrets.token_urlsafe(24), verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _token_request(client_id: str, client_secret: str, data: dict) -> dict:
    auth = (client_id, client_secret) if client_secret else None
    if not client_secret:
        data = {**data, "client_id": client_id}
    try:
        r = httpx.post(f"{API}/oauth2/token", data=data, auth=auth, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise XAmbiguous(f"token endpoint unreachable: {e}") from e
    if r.status_code != 200:
        raise XAuthError(f"token endpoint returned {r.status_code}: {r.text[:300]}")
    return r.json()


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str) -> dict:
    return _token_request(client_id, client_secret, {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    })


def refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return _token_request(client_id, client_secret, {"grant_type": "refresh_token", "refresh_token": refresh_token})


def revoke(client_id: str, client_secret: str, token: str) -> None:
    try:
        httpx.post(f"{API}/oauth2/revoke", data={"token": token, "client_id": client_id},
                   auth=(client_id, client_secret) if client_secret else None, timeout=TIMEOUT)
    except httpx.HTTPError:
        pass


def _user(d: dict) -> XUser:
    return XUser(
        id=d["id"], handle=d.get("username", ""), name=d.get("name", ""), bio=d.get("description", "") or "",
        followers=(d.get("public_metrics") or {}).get("followers_count", 0),
        can_dm=d.get("receives_your_dm"), location=d.get("location", "") or "",
    )


def _event(d: dict) -> DMEvent:
    return DMEvent(id=d["id"], conversation_id=d.get("dm_conversation_id", ""), sender_id=d.get("sender_id", ""),
                   text=d.get("text", ""), created_at=d.get("created_at", ""))


class RealX:
    """`get_token` returns a valid access token (refreshing if needed)."""

    def __init__(self, get_token: Callable[[], str], on_usage: Callable[[str, int], None] | None = None):
        self._get_token = get_token
        self._on_usage = on_usage or (lambda kind, units: None)

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        try:
            r = httpx.request(method, API + path, params=params, json=json, timeout=TIMEOUT,
                              headers={"Authorization": f"Bearer {self._get_token()}"})
        except httpx.HTTPError as e:
            raise XAmbiguous(f"{type(e).__name__}: {e}") from e
        if r.status_code >= 500:
            raise XAmbiguous(f"X returned {r.status_code}: {r.text[:300]}")
        if r.status_code in (200, 201):
            return r.json()
        detail = r.text[:400]
        try:
            body = r.json()
            detail = body.get("detail") or body.get("title") or (body.get("errors") or [{}])[0].get("message") or detail
        except ValueError:
            pass
        if r.status_code == 401:
            raise XAuthError(detail)
        if r.status_code == 403:
            raise XForbidden(detail)
        if r.status_code == 429:
            retry_after = 900.0
            try:
                import time
                retry_after = max(30.0, float(r.headers.get("x-rate-limit-reset", "0")) - time.time())
            except ValueError:
                pass
            raise XRateLimited(detail, retry_after=retry_after)
        raise XError(f"{r.status_code}: {detail}", status=r.status_code)

    def me(self) -> XUser:
        return _user(self._request("GET", "/users/me", params={"user.fields": "description,public_metrics"})["data"])

    def search_recent(self, query: str, max_results: int):
        posts, users, _ = self.search_page(query, max_results)
        return posts, users

    def search_page(self, query: str, max_results: int, next_token: str | None = None):
        params = {
            "query": query, "max_results": max(10, min(100, max_results)),
            "tweet.fields": "created_at,author_id", "expansions": "author_id", "user.fields": USER_FIELDS,
        }
        if next_token:
            params["next_token"] = next_token
        data = self._request("GET", "/tweets/search/recent", params=params)
        users = {u["id"]: _user(u) for u in (data.get("includes") or {}).get("users", [])}
        posts = [
            XPost(id=t["id"], author_id=t["author_id"], text=t.get("text", ""), created_at=t.get("created_at", ""),
                  author_handle=users[t["author_id"]].handle if t["author_id"] in users else "")
            for t in data.get("data", [])
        ]
        self._on_usage("x_post_read", len(posts))
        return posts, users, (data.get("meta") or {}).get("next_token")

    def lookup_usernames(self, handles: list[str]) -> list[XUser]:
        if not handles:
            return []
        data = self._request("GET", "/users/by", params={"usernames": ",".join(handles[:100]), "user.fields": USER_FIELDS})
        users = [_user(u) for u in data.get("data", [])]
        self._on_usage("x_user_read", len(users))
        return users

    def following(self, user_id: str, max_results: int) -> list[XUser]:
        data = self._request("GET", f"/users/{user_id}/following",
                             params={"max_results": max(1, min(1000, max_results)), "user.fields": USER_FIELDS})
        users = [_user(u) for u in data.get("data", [])]
        self._on_usage("x_user_read", len(users))
        return users

    def user_posts(self, user: XUser, max_results: int) -> list[XPost]:
        data = self._request("GET", f"/users/{user.id}/tweets", params={
            "max_results": max(5, min(100, max_results)), "exclude": "retweets,replies", "tweet.fields": "created_at",
        })
        posts = [XPost(id=t["id"], author_id=user.id, text=t.get("text", ""), created_at=t.get("created_at", ""),
                       author_handle=user.handle) for t in data.get("data", [])]
        self._on_usage("x_post_read", len(posts))
        return posts

    def send_dm(self, participant_id: str, text: str) -> SendResult:
        data = self._request("POST", f"/dm_conversations/with/{participant_id}/messages", json={"text": text})["data"]
        return SendResult(event_id=data["dm_event_id"], conversation_id=data["dm_conversation_id"])

    def _dm_params(self, max_results: int) -> dict:
        return {"max_results": max(1, min(100, max_results)), "event_types": "MessageCreate",
                "dm_event.fields": "id,text,created_at,sender_id,dm_conversation_id"}

    def dm_events(self, max_results: int = 100) -> list[DMEvent]:
        events = [_event(e) for e in self._request("GET", "/dm_events", params=self._dm_params(max_results)).get("data", [])]
        self._on_usage("x_dm_read", len(events))
        return events

    def conversation_events(self, participant_id: str) -> list[DMEvent]:
        data = self._request("GET", f"/dm_conversations/with/{participant_id}/dm_events", params=self._dm_params(20))
        events = [_event(e) for e in data.get("data", [])]
        self._on_usage("x_dm_read", len(events))
        return events

    def chat_events(self, conversation_id: str) -> list[ChatEvent]:
        """Newest page of the X Chat history for a 1:1 conversation ("<idA>-<idB>").

        This is where replies typed in the X app show up; the legacy DM endpoints do
        not return them. Bodies from X Chat clients stay encrypted (no keys here).
        """
        try:
            data = self._request("GET", f"/chat/conversations/{conversation_id}/events")
        except XError as e:
            if getattr(e, "status", None) == 404:
                return []  # no conversation yet
            raise
        # Usage is recorded by the caller (services/replies.py), which also rate-limits these reads.
        return [chat_envelope.classify(e) for e in data.get("data", [])]
