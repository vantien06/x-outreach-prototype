"""Live smoke test against the real X API using the account connected in the app.

Usage:  uv run python scripts/live_probe.py <recipient_handle> [--send "text"]

Prints raw status codes, rate-limit headers and response bodies (never the token).
Without --send it only reads (user lookup + existing DM thread).
"""
from __future__ import annotations

import json
import sys

import httpx

from app.clients import _access_token
from app.db import db

API = "https://api.x.com/2"


def call(token: str, method: str, path: str, **kw):
    r = httpx.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, timeout=20, **kw)
    limits = {k: v for k, v in r.headers.items() if k.lower().startswith("x-rate-limit") or k.lower().startswith("x-user-limit")}
    print(f"\n== {method} {path} -> {r.status_code}")
    print("   limits:", json.dumps(limits))
    try:
        body = r.json()
    except ValueError:
        body = r.text[:500]
    print("   body:", json.dumps(body, ensure_ascii=False)[:1200])
    return r.status_code, body


def main() -> None:
    handle = sys.argv[1].lstrip("@")
    text = sys.argv[sys.argv.index("--send") + 1] if "--send" in sys.argv else None
    with db() as conn:
        acct = conn.execute("SELECT * FROM accounts ORDER BY id DESC LIMIT 1").fetchone()
    if not acct:
        sys.exit("No connected account. Sign in with X in the app first.")
    print("sender account:", acct["handle"] if "handle" in acct.keys() else acct["x_user_id"])
    token = _access_token(acct["id"])

    status, body = call(token, "GET", "/users/by", params={
        "usernames": handle, "user.fields": "description,public_metrics,receives_your_dm,connection_status"})
    if status != 200 or not body.get("data"):
        sys.exit("Recipient lookup failed.")
    rid = body["data"][0]["id"]

    params = {"max_results": 20, "event_types": "MessageCreate",
              "dm_event.fields": "id,text,created_at,sender_id,dm_conversation_id"}
    call(token, "GET", f"/dm_conversations/with/{rid}/dm_events", params=params)

    if text:
        call(token, "POST", f"/dm_conversations/with/{rid}/messages", json={"text": text})
        call(token, "GET", f"/dm_conversations/with/{rid}/dm_events", params=params)
        call(token, "GET", "/dm_events", params=params)


if __name__ == "__main__":
    main()
