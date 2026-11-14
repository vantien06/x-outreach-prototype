"""Sign-in gate, account records, and disconnect-and-delete."""
from __future__ import annotations

import hashlib
import hmac

from ..config import get_settings
from ..crypto import decrypt
from ..db import db, iso
from ..x.base import XUser


class NotAllowed(Exception):
    pass


def _code_hash(code: str) -> str:
    return hashlib.sha256(("invite:" + code.strip()).encode()).hexdigest()


def is_allowed(handle: str, x_user_id: str = "") -> bool:
    """True if this account may use the deployment right now, without presenting an invite code:
    local or dry-run mode, a handle on the allowlist, or an account admitted earlier with the
    invite code that is still configured."""
    s = get_settings()
    if s.app_mode != "hosted" or s.dry_run:
        return True
    if handle.lower() in s.allowed_handle_set:
        return True
    if s.invite_code.strip() and x_user_id:
        with db() as conn:
            row = conn.execute("SELECT code_hash FROM invited_accounts WHERE x_user_id=?", (x_user_id,)).fetchone()
        return bool(row) and hmac.compare_digest(row["code_hash"], _code_hash(s.invite_code))
    return False


def check_allowed(handle: str, x_user_id: str = "", invite: str = "") -> None:
    """Sign-in gate. Raises NotAllowed unless the account is allowed or presents the current invite code,
    in which case it is remembered and does not need the code again."""
    if is_allowed(handle, x_user_id):
        return
    s = get_settings()
    code = s.invite_code.strip()
    if code and invite.strip() and hmac.compare_digest(_code_hash(invite), _code_hash(code)):
        if x_user_id:
            with db() as conn:
                conn.execute(
                    "INSERT INTO invited_accounts (x_user_id, handle, code_hash, invited_at) VALUES (?,?,?,?)"
                    " ON CONFLICT(x_user_id) DO UPDATE SET handle=excluded.handle, code_hash=excluded.code_hash,"
                    " invited_at=excluded.invited_at",
                    (x_user_id, handle, _code_hash(code), iso()),
                )
        return
    if invite.strip():
        raise NotAllowed("That invite code is not valid. Check it and sign in again.")
    if code:
        raise NotAllowed(
            f"This tool is invite-only and @{handle} has not been invited yet. "
            "Enter your invite code on this page, then sign in again."
        )
    raise NotAllowed(
        f"@{handle} is not on this deployment's allowlist. Ask the host to add the handle, "
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
