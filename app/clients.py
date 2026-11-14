"""Chooses mock or live clients, and owns X token storage/refresh."""
from __future__ import annotations

import threading
from datetime import timedelta
from functools import lru_cache

from .config import get_settings
from .crypto import decrypt, encrypt
from .db import db, iso, now, parse_iso
from .llm.base import LLMClient
from .services import quota
from .x.base import XAuthError, XClient

_refresh_lock = threading.Lock()


@lru_cache
def get_llm() -> LLMClient:
    s = get_settings()
    if s.dry_run:
        from .llm.mock import MockLLM

        return MockLLM()
    from .llm.real import ClaudeLLM

    return ClaudeLLM(s.anthropic_api_key, s.draft_model, s.fast_model)


def store_tokens(account_id: int, token: dict) -> None:
    expires = now() + timedelta(seconds=int(token.get("expires_in", 7200)))
    with db() as conn:
        conn.execute(
            "UPDATE accounts SET access_token_enc=?, refresh_token_enc=?, token_expires_at=?, needs_reauth=0 WHERE id=?",
            (encrypt(token["access_token"]), encrypt(token.get("refresh_token", "")), iso(expires), account_id),
        )


def _access_token(account_id: int) -> str:
    from .x import real

    s = get_settings()
    with _refresh_lock:
        with db() as conn:
            acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if acc is None:
            raise XAuthError("account no longer exists")
        expires = parse_iso(acc["token_expires_at"])
        if expires and expires - now() > timedelta(seconds=90):
            return decrypt(acc["access_token_enc"])
        refresh_token = decrypt(acc["refresh_token_enc"])
        try:
            if not refresh_token:
                raise XAuthError("no refresh token stored")
            token = real.refresh(s.x_client_id, s.x_client_secret, refresh_token)
        except XAuthError:
            with db() as conn:
                conn.execute("UPDATE accounts SET needs_reauth=1 WHERE id=?", (account_id,))
            raise
        store_tokens(account_id, token)
        return token["access_token"]


def get_x(account) -> XClient:
    if get_settings().dry_run:
        from .x.mock import MockX

        return MockX(account["x_user_id"])
    from .x.real import RealX

    account_id = account["id"]
    return RealX(lambda: _access_token(account_id), lambda kind, units: quota.record(account_id, kind, units))
