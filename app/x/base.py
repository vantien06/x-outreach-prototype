"""The X surface the app depends on. Two implementations: mock.py and real.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class XError(Exception):
    """X definitively rejected the request. Nothing was sent."""

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


class XForbidden(XError):
    """403: not allowed (for a DM: the recipient does not accept DMs from this account)."""


class XRateLimited(XError):
    """429: nothing was sent; back off."""

    def __init__(self, message: str, retry_after: float = 900):
        super().__init__(message)
        self.retry_after = retry_after


class XAuthError(XError):
    """401 or failed token refresh: the account must reconnect."""


class XAmbiguous(Exception):
    """Timeout, dropped connection or 5xx: X may or may not have acted on the request."""


@dataclass
class XUser:
    id: str
    handle: str
    name: str = ""
    bio: str = ""
    followers: int = 0
    # None = X did not tell us. False = X says this account cannot DM them.
    can_dm: bool | None = None
    # Free-text profile location, as the person typed it. Often empty.
    location: str = ""

    @property
    def url(self) -> str:
        return f"https://x.com/{self.handle}"


@dataclass
class XPost:
    id: str
    author_id: str
    text: str
    created_at: str = ""
    author_handle: str = ""

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author_handle or 'i'}/status/{self.id}"


@dataclass
class DMEvent:
    id: str
    conversation_id: str
    sender_id: str
    text: str
    created_at: str = ""


@dataclass
class ChatEvent:
    """One event from the X Chat conversation history (see x/chat_events.py).

    kind:
      message    a message whose text is readable (sent through the legacy DM API)
      encrypted  an end-to-end encrypted message from an X Chat client; text is ""
      activity   an encrypted event that is not a notifying message (seen when a
                 recipient accepts a message request)
      system     conversation housekeeping such as a key change
      unknown    the envelope did not match anything observed so far
    """
    id: str
    conversation_id: str
    sender_id: str
    created_at: str = ""
    kind: str = "unknown"
    text: str = ""


def chat_conversation_id(user_a: str, user_b: str) -> str:
    """X's id for a 1:1 conversation: both user ids, smaller first, joined by '-'."""
    a, b = sorted([str(user_a), str(user_b)], key=lambda v: (len(v), v))
    return f"{a}-{b}"


@dataclass
class SendResult:
    event_id: str
    conversation_id: str


class XClient(Protocol):
    def me(self) -> XUser: ...

    def search_recent(self, query: str, max_results: int) -> tuple[list[XPost], dict[str, XUser]]: ...

    def search_page(self, query: str, max_results: int, next_token: str | None = None
                    ) -> tuple[list[XPost], dict[str, XUser], str | None]:
        """One page of recent search. The third value is the token for the next page, or None."""
        ...

    def lookup_usernames(self, handles: list[str]) -> list[XUser]: ...

    def following(self, user_id: str, max_results: int) -> list[XUser]: ...

    def user_posts(self, user: XUser, max_results: int) -> list[XPost]: ...

    def send_dm(self, participant_id: str, text: str) -> SendResult: ...

    def dm_events(self, max_results: int = 100) -> list[DMEvent]: ...

    def conversation_events(self, participant_id: str) -> list[DMEvent]: ...

    def chat_events(self, conversation_id: str) -> list[ChatEvent]: ...
