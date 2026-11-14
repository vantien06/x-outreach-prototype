"""The LLM surface the app depends on. Two implementations: mock.py and real.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class LLMError(Exception):
    """The model call failed or was declined; no result."""


@dataclass
class EvidenceItem:
    """A stored fact about a candidate. `id` is what claims must cite, e.g. 'E12'."""
    id: str
    kind: str  # bio | post
    text: str
    url: str = ""


@dataclass
class Person:
    handle: str
    name: str
    bio: str
    evidence: list[EvidenceItem]


class Queries(BaseModel):
    queries: list[str] = Field(description="1 to 3 X recent-search queries")


class Fit(BaseModel):
    score: int = Field(description="0-100: how well this person matches the target segment")
    category: Literal["builder", "founder", "researcher", "other"]
    reason: str = Field(description="One or two sentences, using only what the evidence says")
    evidence_ids: list[str] = Field(description="Ids of the evidence items the reason relies on")


class Claim(BaseModel):
    claim: str = Field(description="A statement in the message about the recipient")
    evidence_id: str = Field(description="Id of the evidence item that supports it")


class Draft(BaseModel):
    message: str
    claims: list[Claim]


class ReplyLabel(BaseModel):
    label: Literal["interested", "question", "not_interested", "opt_out", "other"]
    summary: str = Field(description="One short sentence for the operator")


class LLMClient(Protocol):
    def build_queries(self, segment: str) -> Queries: ...

    def score_fit(self, segment: str, person: Person) -> Fit: ...

    def draft_dm(self, pitch: str, sender_handle: str, person: Person) -> Draft: ...

    def classify_reply(self, our_message: str, reply: str) -> ReplyLabel: ...
