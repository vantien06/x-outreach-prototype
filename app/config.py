from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_mode: Literal["local", "hosted"] = "local"
    dry_run: bool = True
    base_url: str = "http://127.0.0.1:8000"
    database_path: str = "data/outreach.db"

    x_client_id: str = ""
    x_client_secret: str = ""
    anthropic_api_key: str = ""
    token_encryption_key: str = ""

    draft_model: str = "claude-sonnet-5-5"
    fast_model: str = "claude-haiku-5-5"

    allowed_handles: str = ""
    # Hosted only: anyone who enters this code on the sign-in page may sign in with any X account.
    # Empty = no invite code; only ALLOWED_HANDLES can sign in. Changing it revokes earlier invitees.
    invite_code: str = ""
    daily_send_cap: int = 20
    send_spacing_seconds: float = 90
    daily_candidate_cap: int = 80
    global_budget_usd: float = 0.0

    max_candidates_per_campaign: int = 15
    # Segment search: up to max_queries queries, posts_per_query posts on the first
    # page of each, and never more than max_posts_per_campaign posts in total
    # (every returned post is billed).
    max_queries: int = 3
    posts_per_query: int = 20
    max_posts_per_campaign: int = 60
    seed_following_limit: int = 100
    # Seed campaigns read this many recent posts per person before scoring (the
    # X endpoint returns at least 5).
    seed_posts_per_candidate: int = 5
    fit_threshold: int = 60

    worker_enabled: bool = True
    worker_tick_seconds: float = 5.0
    # How often the worker looks for conversations that are due a reply check.
    reply_poll_seconds: float = 20
    # X allows 100 chat-history reads per 15 minutes and 10,000 per 24 hours per
    # user (response headers, 2026-11-14). Stay well under both.
    chat_reads_per_15min: int = 60
    chat_reads_per_day: int = 6000
    chat_reads_per_tick: int = 8
    stale_sending_seconds: float = 300

    # Unit cost ESTIMATES in USD, used only for the budget ceiling and the cost
    # panel. The X figures come from third-party summaries of the pay-per-use
    # rate card; check them against your X Developer Console.
    cost_x_post_read: float = 0.005
    cost_x_user_read: float = 0.010
    cost_x_dm_send: float = 0.015
    cost_x_dm_read: float = 0.010
    # Per chat-history request (reply checks). X's price for this endpoint is
    # not known to us; this is a placeholder.
    cost_x_chat_read: float = 0.001
    cost_llm_fast: float = 0.0005
    cost_llm_draft: float = 0.01

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + "/auth/callback"

    @property
    def allowed_handle_set(self) -> set[str]:
        return {h.strip().lstrip("@").lower() for h in self.allowed_handles.split(",") if h.strip()}

    def fernet_key(self) -> bytes:
        if self.token_encryption_key:
            return self.token_encryption_key.encode()
        if self.app_mode == "hosted" and not self.dry_run:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is required in hosted mode")
        from cryptography.fernet import Fernet

        path = Path(self.database_path).parent / "secret.key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(Fernet.generate_key())
        return path.read_bytes().strip()

    def session_secret(self) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(b"session:" + self.fernet_key()).digest()).decode()

    def config_problems(self) -> list[str]:
        """What is still missing before live (non dry-run) use can work."""
        if self.dry_run:
            return []
        out = []
        if not self.x_client_id:
            out.append("X_CLIENT_ID is not set")
        if not self.anthropic_api_key:
            out.append("ANTHROPIC_API_KEY is not set")
        if self.app_mode == "hosted" and not self.allowed_handle_set:
            out.append("ALLOWED_HANDLES is empty, so nobody can sign in")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
