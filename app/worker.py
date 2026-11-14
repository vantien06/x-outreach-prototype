"""In-process background worker: sends approved DMs (paced, capped) and polls for replies.

Single instance only: run exactly one app process per database.
"""
from __future__ import annotations

import logging
import threading

from .clients import get_llm, get_x
from .config import get_settings
from .db import db, iso, now, parse_iso
from .services import outreach, replies
from .x.base import XAmbiguous, XAuthError, XError, XRateLimited

log = logging.getLogger("worker")


def tick() -> None:
    s = get_settings()
    with db() as conn:
        accounts = conn.execute("SELECT * FROM accounts WHERE needs_reauth=0").fetchall()
    for acc in accounts:
        x = get_x(acc)
        try:
            result = outreach.send_tick(acc["id"], x)
            if result:
                log.info("account %s: send -> %s", acc["handle"], result)
        except Exception:
            log.exception("send tick failed for account %s", acc["handle"])

        poll_every = min(s.reply_poll_seconds, 2) if s.dry_run else s.reply_poll_seconds
        last = parse_iso(acc["last_reply_poll_at"])
        if last and (now() - last).total_seconds() < poll_every:
            continue
        with db() as conn:
            waiting = conn.execute(
                "SELECT 1 FROM outreach WHERE account_id=? AND state IN ('sent','needs_check') LIMIT 1", (acc["id"],)
            ).fetchone()
        if not waiting:
            continue  # nothing to listen for; do not pay for DM reads
        error = None
        try:
            replies.poll(acc, x, get_llm())
        except XRateLimited as e:
            error = f"X rate limit while checking for replies; next attempt later. ({e})"
        except XAuthError:
            error = "X sign-in expired. Reconnect the account to keep tracking replies."
        except (XError, XAmbiguous) as e:
            error = f"Could not check conversations on X for replies: {e}"
        except Exception as e:
            log.exception("reply poll failed for account %s", acc["handle"])
            error = f"Unexpected error while checking for replies: {type(e).__name__}"
        with db() as conn:
            conn.execute("UPDATE accounts SET last_reply_poll_at=?, last_poll_error=? WHERE id=?",
                         (iso(), error, acc["id"]))


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="outreach-worker")
        self._stop_event = threading.Event()

    def run(self) -> None:
        s = get_settings()
        while not self._stop_event.is_set():
            try:
                outreach.recover_stale_sending()
                tick()
            except Exception:
                log.exception("worker tick failed")
            self._stop_event.wait(min(s.worker_tick_seconds, 1.0) if s.dry_run else s.worker_tick_seconds)

    def stop(self) -> None:
        self._stop_event.set()
