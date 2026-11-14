"""Dry-run LLM: deterministic keyword heuristics, no network."""
from __future__ import annotations

import re

from .base import Claim, Draft, Fit, Person, Queries, ReplyLabel

KEYWORDS = ["robot", "embodied", "llm", "inference", "model", "agent", "ml ", "asr", "tts", "vision",
            "policy", "drone", "ai ", "gpu", "eval", "manipulation", "speech", "dataset"]
NEGATIVE = ["trading signals", "100x", "food blogger", "bot account", "automated feed", "promo"]


def _first_sentence(text: str, limit: int = 110) -> str:
    s = re.split(r"(?<=[.!?])\s", text.strip())[0]
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


class MockLLM:
    def build_queries(self, segment: str) -> Queries:
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", segment)][:4] or ["robotics", "AI"]
        return Queries(queries=["(" + " OR ".join(words) + ") -is:retweet -is:reply"])

    def score_fit(self, segment: str, person: Person) -> Fit:
        blob = (person.bio + " " + " ".join(e.text for e in person.evidence)).lower() + " "
        hits = [k.strip() for k in KEYWORDS if k in blob]
        bad = [k for k in NEGATIVE if k in blob]
        bio_ev = next((e for e in person.evidence if e.kind == "bio"), None)
        post_ev = next((e for e in person.evidence if e.kind == "post"), None)
        cited = [e.id for e in (bio_ev, post_ev) if e]
        if bad:
            return Fit(score=10, category="other", evidence_ids=cited[:1],
                       reason=f"Not a builder profile: the bio reads \"{_first_sentence(person.bio, 80)}\".")
        score = min(95, 50 + 12 * len(hits)) if hits else 30
        low = person.bio.lower()
        category = ("founder" if any(w in low for w in ("founder", "cto", "co-founder"))
                    else "researcher" if any(w in low for w in ("phd", "research"))
                    else "builder" if hits else "other")
        reason = f"Bio: \"{_first_sentence(person.bio, 90)}\""
        if post_ev:
            reason += f" Recent post: \"{_first_sentence(post_ev.text, 90)}\""
        return Fit(score=score, category=category, reason=reason, evidence_ids=cited)

    def draft_dm(self, pitch: str, sender_handle: str, person: Person) -> Draft:
        first = (person.name or person.handle).split(" ")[0]
        post_ev = next((e for e in person.evidence if e.kind == "post"), None)
        bio_ev = next((e for e in person.evidence if e.kind == "bio"), None)
        claims = []
        if post_ev:
            snippet = _first_sentence(post_ev.text, 100)
            opener = f"Hi {first}, I read your post: \"{snippet}\""
            claims.append(Claim(claim=f"You posted: \"{snippet}\"", evidence_id=post_ev.id))
        elif bio_ev:
            snippet = _first_sentence(bio_ev.text, 100)
            opener = f"Hi {first}, your bio says: \"{snippet}\""
            claims.append(Claim(claim=f"Your bio says: \"{snippet}\"", evidence_id=bio_ev.id))
        else:
            opener = f"Hi {first},"
        body = pitch.strip() or "We back AI and robotics builders in Southeast Asia."
        return Draft(message=f"{opener}\n\n{body}\n\nWould you be open to a short chat? No worries if not.",
                     claims=claims)

    def classify_reply(self, our_message: str, reply: str) -> ReplyLabel:
        low = reply.lower()
        if any(w in low for w in ("don't contact", "do not contact", "stop", "unsubscribe", "remove me")):
            return ReplyLabel(label="opt_out", summary="Asked not to be contacted again.")
        if any(w in low for w in ("not looking", "no thanks", "not interested")):
            return ReplyLabel(label="not_interested", summary="Declined politely.")
        if "?" in reply and any(w in low for w in ("happy", "love to", "sure", "great")):
            return ReplyLabel(label="interested", summary="Open to talking and asked a question.")
        if "?" in reply or "details" in low:
            return ReplyLabel(label="question", summary="Asked for more information.")
        if any(w in low for w in ("happy", "love to", "sure", "sounds good")):
            return ReplyLabel(label="interested", summary="Open to talking.")
        return ReplyLabel(label="other", summary="Replied; read it.")
