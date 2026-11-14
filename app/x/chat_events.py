"""Read the unencrypted envelope of an X Chat event (`encoded_event`).

`GET /2/chat/conversations/{id}/events` returns each event as base64 of a Thrift
(binary protocol) struct. Message bodies sent from X Chat clients are end-to-end
encrypted and this module never tries to decrypt them: it only reads the envelope
to tell WHAT KIND of event it is, and the plaintext body when there is one
(messages sent through the legacy DM endpoint are stored unencrypted).

The field layout below is not documented by X. It was derived from live events
on 2026-11-14 (see `classify`), so anything that does not match exactly is
reported as kind "unknown" and the caller treats it conservatively.
"""
from __future__ import annotations

import base64
import struct

from .base import ChatEvent

STOP, BOOL, BYTE, DOUBLE, I16, I32, I64, STRING, STRUCT, MAP, SET, LIST = 0, 2, 3, 4, 6, 8, 10, 11, 12, 13, 14, 15
MAX_DEPTH = 12


class ThriftError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ThriftError("truncated")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def value(self, ttype: int, depth: int):
        if depth > MAX_DEPTH:
            raise ThriftError("too deep")
        if ttype == BOOL:
            return self.take(1) != b"\x00"
        if ttype == BYTE:
            return self.take(1)[0]
        if ttype == DOUBLE:
            return struct.unpack(">d", self.take(8))[0]
        if ttype == I16:
            return struct.unpack(">h", self.take(2))[0]
        if ttype == I32:
            return struct.unpack(">i", self.take(4))[0]
        if ttype == I64:
            return struct.unpack(">q", self.take(8))[0]
        if ttype == STRING:
            return self.take(struct.unpack(">i", self.take(4))[0])
        if ttype == STRUCT:
            return self.struct(depth + 1)
        if ttype in (LIST, SET):
            etype = self.take(1)[0]
            n = struct.unpack(">i", self.take(4))[0]
            if n < 0 or n > 10_000:
                raise ThriftError("bad list size")
            return [self.value(etype, depth + 1) for _ in range(n)]
        if ttype == MAP:
            ktype, vtype = self.take(1)[0], self.take(1)[0]
            n = struct.unpack(">i", self.take(4))[0]
            if n < 0 or n > 10_000:
                raise ThriftError("bad map size")
            return [(self.value(ktype, depth + 1), self.value(vtype, depth + 1)) for _ in range(n)]
        raise ThriftError(f"unknown thrift type {ttype}")

    def struct(self, depth: int = 0) -> dict[int, object]:
        out: dict[int, object] = {}
        while True:
            ttype = self.take(1)[0]
            if ttype == STOP:
                return out
            fid = struct.unpack(">h", self.take(2))[0]
            out[fid] = self.value(ttype, depth)


def parse(encoded_event: str) -> dict[int, object]:
    """Decode base64 + Thrift into nested {field id: value} dicts. Raises ThriftError."""
    try:
        raw = base64.b64decode(encoded_event + "=" * (-len(encoded_event) % 4))
    except ValueError as e:
        raise ThriftError(f"bad base64: {e}") from e
    try:
        return _Reader(raw).struct()
    except (IndexError, struct.error) as e:
        raise ThriftError(str(e)) from e


def _plain_text(content: bytes) -> str | None:
    """Text of an unencrypted message body, or None if `content` is not one.

    Observed shape of an unencrypted body: Thrift {1: {1: {1: <utf-8 text>}}},
    consuming every byte. Ciphertext does not parse this way.
    """
    reader = _Reader(content)
    try:
        body = reader.struct()
    except (ThriftError, IndexError, struct.error):
        return None
    if reader.pos != len(content):
        return None
    try:
        text = body[1][1][1]
    except (KeyError, TypeError):
        return None
    if not isinstance(text, bytes):
        return None
    try:
        return text.decode("utf-8")
    except UnicodeDecodeError:
        return None


def classify(event: dict) -> ChatEvent:
    """Turn one item of `GET /2/chat/conversations/{id}/events` into a ChatEvent.

    Derived from five live events (2026-11-14), top-level field 7 is a union:
      7.1  message event. 7.1.100 is the body: a plain Thrift struct for messages
           sent through the legacy DM endpoint, ciphertext for X Chat clients.
           7.1.102 was True on every real message (plain and encrypted) and False
           on the small encrypted event a recipient produced when accepting a
           message request, so it is read as "this is a notifying message".
      7.3  conversation key change (carries per-participant wrapped keys).
    The 7.1.102 reading rests on one accepted request and one reply, so an
    encrypted event with 102 unset/False is reported as "activity", never as a reply.
    """
    out = ChatEvent(
        id=str(event.get("id", "")), conversation_id=str(event.get("conversation_id", "")).replace(":", "-"),
        sender_id=str(event.get("sender_id", "")), created_at=event.get("created_at", "") or "",
    )
    try:
        union = parse(event.get("encoded_event") or "").get(7)
    except ThriftError:
        return out
    if not isinstance(union, dict) or not union:
        return out
    if 1 not in union:
        out.kind = "system"
        return out
    message = union[1]
    content = message.get(100) if isinstance(message, dict) else None
    if not isinstance(content, bytes):
        return out
    text = _plain_text(content)
    if text is not None:
        out.kind, out.text = "message", text
    elif message.get(102) is True:
        out.kind = "encrypted"
    else:
        out.kind = "activity"
    return out
