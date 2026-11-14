"""Find candidates for a campaign and score them, keeping the evidence each score rests on.

Segment campaigns: the LLM writes several search queries of different breadth, we read a bounded
number of recent posts, drop obvious non-people with cheap rules, rank what is left (reachable
people with the most evidence first) and only then pay for LLM scoring.

Seed campaigns: we take who the seed accounts follow, apply the same rules and ranking, and read a
few recent posts per person before scoring so the score rests on more than a bio.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..config import get_settings
from ..db import db, iso
from ..llm.base import EvidenceItem, LLMClient, LLMError, Person
from ..x.base import XAmbiguous, XAuthError, XClient, XError, XPost, XRateLimited, XUser
from . import quota
from .outreach import add_post_evidence

DEFAULT_SEGMENT = (
    "AI and robotics builders, founders and researchers: people working on AI models, applications, "
    "developer tools, infrastructure, robotics or embodied AI."
)

# Cheap pre-filters. They only drop accounts that are very unlikely to be an individual
# practitioner, so that LLM scoring (and the daily candidate allowance) is spent on real people.
_ORG_NAME = re.compile(
    r"\b(news|daily|weekly|magazine|media|official|jobs|careers|newsletter|headlines|updates|alerts|bot)\b", re.I)
_ORG_BIO = re.compile(
    r"^\W*(the\s+)?official\b|\bofficial (account|page|x account|twitter)\b|\bautomated (feed|account)\b"
    r"|\bbot account\b|\bnews (feed|aggregator|headlines)\b", re.I)
_SPAM = re.compile(
    r"\b(giveaway|airdrop|100x|presale|trading signals?|dm for promo|promo code|follow back|onlyfans|casino)\b", re.I)
_URL = re.compile(r"https?://\S+")


@dataclass
class Found:
    user: XUser
    source: str
    posts: list[XPost] = field(default_factory=list)
    queries: set[str] = field(default_factory=set)


def parse_handles(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[\s,;]+", text):
        h = raw.strip().rstrip("/").split("/")[-1].lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) and h.lower() not in (x.lower() for x in out):
            out.append(h)
    return out


def clean_queries(queries: list[str], limit: int) -> list[str]:
    """Trim, drop empties and duplicates, and make sure reposts are excluded (they bill without adding authors)."""
    out: list[str] = []
    for q in queries:
        q = " ".join((q or "").split())
        if not q:
            continue
        if "-is:retweet" not in q:
            q += " -is:retweet"
        if q.lower() not in (o.lower() for o in out):
            out.append(q)
    return out[:limit]


def prefilter_reason(user: XUser, posts: list[XPost], *, need_text: bool) -> str | None:
    """Why this account is not worth scoring, or None. `need_text`: require a bio or some post text."""
    post_text = " ".join(p.text for p in posts)
    if _SPAM.search(user.bio) or (posts and all(_SPAM.search(p.text) for p in posts)):
        return "promotion or spam"
    if _ORG_NAME.search(user.name) or _ORG_BIO.search(user.bio):
        return "organisation, news or bot account"
    if need_text and not user.bio.strip() and len(_URL.sub("", post_text).strip()) < 60:
        return "no bio and almost no text"
    return None


def rank_key(f: Found) -> tuple:
    """Higher sorts first: people X says we can DM, then matched by more queries, more posts, with a bio."""
    return (f.user.can_dm is not False, len(f.queries), min(len(f.posts), 3), bool(f.user.bio.strip()))


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


def _search_segment(segment: str, x: XClient, llm: LLMClient, account_id: int, want: int,
                    skip: set[str], notes: list[str], cost: dict) -> dict[str, Found]:
    """Run the LLM's queries within the post budget. `want` is how many scorable people we are aiming for."""
    s = get_settings()
    queries = clean_queries(llm.build_queries(segment).queries, s.max_queries)
    quota.record(account_id, "llm_fast", 1)
    cost["llm_fast"] += 1
    if not queries:
        raise LLMError("the model returned no search queries")

    found: dict[str, Found] = {}
    seen_posts: set[str] = set()
    left = s.max_posts_per_campaign
    more: list[tuple[int, str, str]] = []   # (new authors on first page, query, next_token)
    failed: list[str] = []

    def page(q: str, token: str | None) -> tuple[int, str | None]:
        nonlocal left
        # 10 is the smallest page X serves; every returned post is billed.
        posts, users, nxt = x.search_page(q, max(10, min(s.posts_per_query, left)), token)
        left -= len(posts)
        cost["posts"] += len(posts)
        new_authors = 0
        for p in posts:
            u = users.get(p.author_id)
            if not u or p.id in seen_posts:
                continue
            seen_posts.add(p.id)
            if u.id not in found:
                found[u.id] = Found(user=u, source=f"search: {q}")
                new_authors += u.id not in skip
            found[u.id].posts.append(p)
            found[u.id].queries.add(q)
        return new_authors, nxt

    for q in queries:
        if left < 10:
            break
        try:
            new_authors, nxt = page(q, None)
        except (XAuthError, XRateLimited):
            raise
        except XError as e:
            # One malformed or refused query must not sink the whole search.
            failed.append(f"{q} ({e})")
            continue
        if nxt:
            more.append((new_authors, q, nxt))
    if failed and len(failed) == len(queries):
        raise XError("every search query was refused by X: " + " | ".join(failed))

    # Second pages, most productive query first, while we are short of people and have post budget.
    for _, q, token in sorted(more, reverse=True):
        usable = sum(1 for k in found if k not in skip)
        if left < 10 or usable >= want * 2:
            break
        try:
            page(q, token)
        except (XAuthError, XRateLimited):
            raise
        except XError:
            continue

    notes.append("Queries used: " + " | ".join(q for q in queries if not any(f.startswith(q) for f in failed)))
    if failed:
        notes.append("Queries X refused: " + " | ".join(failed))
    return found


def _follow_seeds(seeds_text: str, x: XClient, notes: list[str], cost: dict) -> dict[str, Found]:
    s = get_settings()
    handles = parse_handles(seeds_text)
    seeds = x.lookup_usernames(handles)
    cost["users"] += len(seeds)
    missing = {h.lower() for h in handles} - {u.handle.lower() for u in seeds}
    if missing:
        notes.append("Seed accounts not found: " + ", ".join("@" + m for m in sorted(missing)))
    found: dict[str, Found] = {}
    per_seed = max(1, s.seed_following_limit // max(1, len(seeds)))
    for seed in seeds:
        following = x.following(seed.id, per_seed)
        cost["users"] += len(following)
        for u in following:
            found.setdefault(u.id, Found(user=u, source=f"followed by @{seed.handle}"))
    return found


def _read_posts(f: Found, x: XClient, cost: dict, state: dict) -> str:
    """Read a few recent posts for a seed-derived person. Returns a note for the operator ('' if posts were read)."""
    s = get_settings()
    if state.get("stop"):
        return state["stop"]
    try:
        f.posts = x.user_posts(f.user, s.seed_posts_per_candidate)[: s.seed_posts_per_candidate]
    except XRateLimited:
        state["stop"] = "X rate-limited post reads during this search, so their posts were not read."
        return state["stop"]
    except XAuthError as e:
        state["auth_errors"] = state.get("auth_errors", 0) + 1
        if state["auth_errors"] >= 3:
            raise   # three in a row is the token, not three protected accounts
        return f"X did not allow their posts to be read ({e}); the account may be protected."
    except (XError, XAmbiguous) as e:
        return f"X did not return their posts ({e})."
    state["auth_errors"] = 0
    cost["posts"] += len(f.posts)
    if not f.posts:
        return "X returned no recent original posts for them (protected, or only replies and reposts)."
    return ""


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
    skip = known | {camp["me"]}
    notes: list[str] = []
    cost = {"posts": 0, "users": 0, "llm_fast": 0}
    is_segment = camp["mode"] == "segment"

    if is_segment:
        segment = camp["segment"]
        found = _search_segment(segment, x, llm, account_id, budget, skip, notes, cost)
    else:
        segment = camp["segment"] or DEFAULT_SEGMENT
        found = _follow_seeds(camp["seeds"], x, notes, cost)

    fresh = [f for k, f in found.items() if k not in skip]
    skipped_known = len(found) - len(fresh)

    kept, dropped = [], []
    for f in fresh:
        why = prefilter_reason(f.user, f.posts, need_text=is_segment)
        (dropped if why else kept).append((f, why))
    kept = sorted((f for f, _ in kept), key=rank_key, reverse=True)
    if len(kept) > budget:
        notes.append(f"{len(kept) - budget} more people were found but not scored (limit {budget} per run); "
                     "people X says you can message and people with more matching posts were scored first.")
    kept = kept[:budget]
    if dropped:
        shown = ", ".join(f"@{f.user.handle} ({why})" for f, why in dropped[:6])
        notes.append(f"{len(dropped)} left out before scoring: {shown}" + (" and others." if len(dropped) > 6 else "."))
    if skipped_known:
        notes.append(f"{skipped_known} already in an earlier campaign or your own account, skipped.")

    scored = 0
    post_state: dict = {}
    for f in kept:
        user = f.user
        posts_note = "" if is_segment else _read_posts(f, x, cost, post_state)
        with db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO candidates (account_id, campaign_id, x_user_id, handle, name, bio, followers,"
                " can_dm, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (account_id, campaign_id, user.id, user.handle, user.name, user.bio, user.followers,
                 None if user.can_dm is None else int(user.can_dm), f.source, iso()),
            )
            if not cur.rowcount:
                continue
            cid = cur.lastrowid
            if user.bio.strip():
                conn.execute(
                    "INSERT OR IGNORE INTO evidence (account_id, candidate_id, kind, ref_id, url, text)"
                    " VALUES (?,?,?,?,?,?)", (account_id, cid, "bio", "bio", user.url, user.bio),
                )
            if user.location.strip():
                # Stored as a second 'bio' item so scoring and drafts can cite where the person says they are.
                conn.execute(
                    "INSERT OR IGNORE INTO evidence (account_id, candidate_id, kind, ref_id, url, text)"
                    " VALUES (?,?,?,?,?,?)",
                    (account_id, cid, "bio", "location", user.url, f"Profile location: {user.location.strip()}"),
                )
            add_post_evidence(conn, account_id, cid, f.posts[:5])
            rows = conn.execute("SELECT * FROM evidence WHERE candidate_id=? ORDER BY kind, id", (cid,)).fetchall()
        evidence = [EvidenceItem(id=f"E{r['id']}", kind=r["kind"], text=r["text"], url=r["url"]) for r in rows]
        if not user.bio.strip() and not f.posts:
            # Nothing to judge: say why instead of paying for a score that could only be a guess.
            with db() as conn:
                conn.execute(
                    "UPDATE candidates SET fit_reason=?, status='unscored' WHERE id=?",
                    ("Not scored: this profile has no bio, and " +
                     (posts_note[0].lower() + posts_note[1:] if posts_note else "no posts were available."), cid))
            continue
        try:
            fit = llm.score_fit(segment, Person(handle=user.handle, name=user.name, bio=user.bio, evidence=evidence))
        except LLMError as e:
            with db() as conn:
                conn.execute("UPDATE candidates SET fit_reason=?, status='unscored' WHERE id=?",
                             (f"Could not be scored: {e}", cid))
            continue
        quota.record(account_id, "llm_fast", 1)
        quota.note_candidate_scored(account_id)
        cost["llm_fast"] += 1
        valid = {e.id for e in evidence}
        reason = fit.reason + (f" Note: {posts_note}" if posts_note else "")
        with db() as conn:
            conn.execute(
                "UPDATE candidates SET fit_score=?, fit_category=?, fit_reason=?, fit_evidence_ids=? WHERE id=?",
                (max(0, min(100, fit.score)), fit.category, reason,
                 json.dumps([i for i in fit.evidence_ids if i in valid]), cid),
            )
        scored += 1

    if not found:
        notes.insert(0, "No people found. Try a broader segment description or different seed accounts.")
    notes.append(_cost_note(cost, scored))
    _set_status(campaign_id, "ready", "\n".join(notes))


def _cost_note(cost: dict, scored: int) -> str:
    s = get_settings()
    parts = []
    if cost["posts"]:
        parts.append(f"{cost['posts']} posts read")
    if cost["users"]:
        parts.append(f"{cost['users']} profiles read")
    parts.append(f"{scored} {'person' if scored == 1 else 'people'} scored")
    if s.dry_run:
        return "Dry run: nothing was billed (" + ", ".join(parts) + ")."
    usd = (cost["posts"] * s.cost_x_post_read + cost["users"] * s.cost_x_user_read
           + cost["llm_fast"] * s.cost_llm_fast)
    return f"Estimated cost of this search: ${usd:.2f} (" + ", ".join(parts) + "; X prices are estimates)."
