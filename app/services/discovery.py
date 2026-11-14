"""Find candidates for a campaign and score them, keeping the evidence each score rests on."""
from __future__ import annotations

import json
import re

from ..config import get_settings
from ..db import db, iso
from ..llm.base import EvidenceItem, LLMClient, LLMError, Person
from ..x.base import XAmbiguous, XClient, XError, XPost, XUser
from . import quota
from .outreach import add_post_evidence


def parse_handles(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[\s,;]+", text):
        h = raw.strip().rstrip("/").split("/")[-1].lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) and h.lower() not in (x.lower() for x in out):
            out.append(h)
    return out


def _set_status(campaign_id: int, status: str, note: str = "") -> None:
    with db() as conn:
        conn.execute("UPDATE campaigns SET status=?, note=? WHERE id=?", (status, note, campaign_id))


def run_campaign(campaign_id: int, x: XClient, llm: LLMClient) -> None:
    """Background job. Always leaves the campaign in 'ready' or 'failed' with a note explaining what happened."""
    try:
        _run(campaign_id, x, llm)
    except quota.LimitReached as e:
        _set_status(campaign_id, "failed", str(e))
    except (XError, XAmbiguous) as e:
        _set_status(campaign_id, "failed", f"X API problem during discovery: {e}")
    except LLMError as e:
        _set_status(campaign_id, "failed", f"LLM problem during discovery: {e}")
    except Exception as e:
        _set_status(campaign_id, "failed", f"Unexpected error: {type(e).__name__}: {e}")


def _run(campaign_id: int, x: XClient, llm: LLMClient) -> None:
    s = get_settings()
    with db() as conn:
        camp = conn.execute(
            "SELECT k.*, a.x_user_id AS me FROM campaigns k JOIN accounts a ON a.id=k.account_id WHERE k.id=?",
            (campaign_id,),
        ).fetchone()
        quota.check_budget(conn)
        left = quota.candidates_left(conn, camp["account_id"])
        known = {r["x_user_id"] for r in conn.execute(
            "SELECT x_user_id FROM candidates WHERE account_id=?", (camp["account_id"],))}
    account_id = camp["account_id"]
    if left == 0:
        raise quota.LimitReached(
            f"Daily limit of {s.daily_candidate_cap} scored candidates reached for this account. Try again tomorrow (UTC)."
        )
    budget = s.max_candidates_per_campaign if left is None else min(left, s.max_candidates_per_campaign)

    found: dict[str, tuple[XUser, list[XPost], str]] = {}
    notes: list[str] = []
    if camp["mode"] == "segment":
        queries = llm.build_queries(camp["segment"]).queries[:3]
        quota.record(account_id, "llm_fast", 1)
        for q in queries:
            posts, users = x.search_recent(q, s.posts_per_query)
            for p in posts:
                u = users.get(p.author_id)
                if u:
                    found.setdefault(u.id, (u, [], f"search: {q}"))[1].append(p)
        notes.append("Queries used: " + " | ".join(queries))
        segment = camp["segment"]
    else:
        handles = parse_handles(camp["seeds"])
        seeds = x.lookup_usernames(handles)
        missing = {h.lower() for h in handles} - {u.handle.lower() for u in seeds}
        if missing:
            notes.append("Seed accounts not found: " + ", ".join("@" + m for m in sorted(missing)))
        per_seed = max(1, s.seed_following_limit // max(1, len(seeds)))
        for seed in seeds:
            for u in x.following(seed.id, per_seed):
                found.setdefault(u.id, (u, [], f"followed by @{seed.handle}"))
        segment = camp["segment"] or (
            "AI and robotics builders, founders and researchers: people working on AI models, applications, "
            "developer tools, infrastructure, robotics or embodied AI."
        )

    fresh = [v for k, v in found.items() if k not in known and k != camp["me"]]
    skipped_known = len(found) - len(fresh)
    # People with more matching posts first: more evidence to judge and to personalise with.
    fresh.sort(key=lambda t: len(t[1]), reverse=True)
    if len(fresh) > budget:
        notes.append(f"{len(fresh) - budget} more people were found but not scored (limit {budget} per run).")
    fresh = fresh[:budget]
    if skipped_known:
        notes.append(f"{skipped_known} already in an earlier campaign or your own account, skipped.")

    scored = 0
    for user, posts, source in fresh:
        with db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO candidates (account_id, campaign_id, x_user_id, handle, name, bio, followers,"
                " can_dm, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (account_id, campaign_id, user.id, user.handle, user.name, user.bio, user.followers,
                 None if user.can_dm is None else int(user.can_dm), source, iso()),
            )
            if not cur.rowcount:
                continue
            cid = cur.lastrowid
            if user.bio.strip():
                conn.execute(
                    "INSERT OR IGNORE INTO evidence (account_id, candidate_id, kind, ref_id, url, text)"
                    " VALUES (?,?,?,?,?,?)", (account_id, cid, "bio", "bio", user.url, user.bio),
                )
            add_post_evidence(conn, account_id, cid, posts[:5])
            rows = conn.execute("SELECT * FROM evidence WHERE candidate_id=? ORDER BY kind, id", (cid,)).fetchall()
        evidence = [EvidenceItem(id=f"E{r['id']}", kind=r["kind"], text=r["text"], url=r["url"]) for r in rows]
        try:
            fit = llm.score_fit(segment, Person(handle=user.handle, name=user.name, bio=user.bio, evidence=evidence))
        except LLMError as e:
            with db() as conn:
                conn.execute("UPDATE candidates SET fit_reason=?, status='unscored' WHERE id=?",
                             (f"Could not be scored: {e}", cid))
            continue
        quota.record(account_id, "llm_fast", 1)
        quota.note_candidate_scored(account_id)
        valid = {e.id for e in evidence}
        with db() as conn:
            conn.execute(
                "UPDATE candidates SET fit_score=?, fit_category=?, fit_reason=?, fit_evidence_ids=? WHERE id=?",
                (max(0, min(100, fit.score)), fit.category, fit.reason,
                 json.dumps([i for i in fit.evidence_ids if i in valid]), cid),
            )
        scored += 1

    if not found:
        notes.insert(0, "No people found. Try a broader segment description or different seed accounts.")
    _set_status(campaign_id, "ready", " ".join(notes))
