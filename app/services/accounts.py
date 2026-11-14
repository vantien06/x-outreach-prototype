"""Sign-in gate, account records, and disconnect-and-delete."""
from __future__ import annotations

from ..config import get_settings
from ..crypto import decrypt
from ..db import db, iso
from ..x.base import XUser


class NotAllowed(Exception):
    pass


def check_allowed(handle: str) -> None:
    s = get_settings()
    if s.app_mode != "hosted" or s.dry_run:
        return
    if handle.lower() not in s.allowed_handle_set:
        raise NotAllowed(
            f"@{handle} is not on this deployment's allowlist. Ask the host to add the handle to ALLOWED_HANDLES, "
            "or run the app locally with your own keys."
        )


def upsert(user: XUser) -> int:
    with db() as conn:
        conn.execute(
            "INSERT INTO accounts (x_user_id, handle, name, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(x_user_id) DO UPDATE SET handle=excluded.handle, name=excluded.name",
            (user.id, user.handle, user.name, iso()),
        )
        return conn.execute("SELECT id FROM accounts WHERE x_user_id=?", (user.id,)).fetchone()["id"]


def get(account_id: int | None):
    if not account_id:
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def set_paused(account_id: int, paused: bool) -> None:
    with db() as conn:
        conn.execute("UPDATE accounts SET paused=? WHERE id=?", (int(paused), account_id))


def disconnect_and_delete(account_id: int) -> None:
    """Revoke the X token (best effort) and delete everything stored for this account.

    What deliberately remains: contact_tombstones (hashes of sender/recipient id pairs,
    so reconnecting cannot message the same people again) and the cost ledger rows,
    detached from the account.
    """
    s = get_settings()
    acc = get(account_id)
    if acc is None:
        return
    if not s.dry_run:
        from ..x import real

        for enc in (acc["refresh_token_enc"], acc["access_token_enc"]):
            try:
                token = decrypt(enc)
            except Exception:
                token = ""
            if token:
                real.revoke(s.x_client_id, s.x_client_secret, token)
    with db() as conn:
        conn.execute("DELETE FROM mock_dm_events WHERE owner_x_user_id=?", (acc["x_user_id"],))
        conn.execute("UPDATE usage_ledger SET account_id=NULL WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))  # cascades to all per-account tables
