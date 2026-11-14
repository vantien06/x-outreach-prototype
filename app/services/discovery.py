"""Find candidates for a campaign and score them, keeping the evidence each score rests on.

Segment campaigns: the LLM writes search queries (first-person "I'm building ..." phrasing ANDed
with the segment's terms) plus two word lists: signs that an author does this work, and signs that
an account only talks about it. We read a bounded number of recent posts, drop obvious non-people
with cheap rules, give everyone left a free pre-score from those word lists over their bio and
posts, rank by it, and only then pay for LLM scoring of the top of the list.

Seed campaigns: we take who the seed accounts follow, apply the same rules and ranking (with the
built-in word lists only), and read a few recent posts per person before scoring so the score
rests on more than a bio.
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
# Matched against whole tokens of the display name and the handle. Tokens are split on anything
# that is not a letter or digit, on underscores and on camelCase, so "ZOOMEX_Official",
# "AgentsDaily" and "orchid_hq" are caught while "Robotics", "Abbot" or "Newsome" are not.
_ORG_TOKENS = frozenset((
    "news", "daily", "weekly", "magazine", "media", "official", "jobs", "careers", "newsletter",
    "headlines", "updates", "alerts", "bot", "hq", "exchange", "podcast", "conference", "summit",
    "digest", "press", "tv", "radio", "deals", "giveaways", "airdrops",
))
# Run-together lowercase handles, e.g. "zoomexofficial". Kept to endings that are not surnames.
_ORG_HANDLE_SUFFIX = ("official", "news", "daily", "weekly", "hq")
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_ORG_BIO = re.compile(
    r"^\W*(the\s+)?official\b|\bofficial (account|page|x account|twitter)\b|\bautomated (feed|account)\b"
    r"|\bbot account\b|\bnews (feed|aggregator|headlines)\b", re.I)
_SPAM = re.compile(
    r"\b(giveaway|airdrop|100x|presale|trading signals?|dm for promo|promo code|follow back|onlyfans|casino)\b", re.I)
_URL = re.compile(r"https?://\S+")


# Built-in word lists for the free pre-score. The LLM adds segment-specific ones for segment
# campaigns; seed campaigns use these alone.
BUILDER_TERMS = (
    "founder", "co-founder", "cofounder", "cto", "building", "i'm building", "we're building", "i built",
    "we built", "we shipped", "just shipped", "shipping", "my startup", "side project", "open source",
    "open-sourced", "engineer", "developer", "researcher", "research engineer", "phd", "maintainer", "indie hacker",
)
OFF_TARGET_TERMS = (
    "news", "newsletter", "curator", "curated", "curating", "daily updates", "latest updates", "headlines",
    "exchange", "trading", "airdrop", "giveaway", "casino", "online course", "courses", "bootcamp", "enroll",
    "recruiter", "recruiting", "job board", "conference", "summit", "event organiser",
    "event organizer", "official account", "official page", "marketing agency", "influencer", "affiliate",
    "wellness", "lifestyle",
)


@dataclass
class Found:
    user: XUser
    source: str
    posts: list[XPost] = field(default_factory=list)
    queries: set[str] = field(default_factory=set)
    pre: int = 0                       # free pre-score, see prescore()
    off: list[str] = field(default_factory=list)   # off-target terms that matched


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens, splitting on punctuation, underscores and camelCase."""
    return {t.lower() for t in _TOKEN.findall(text or "")}


def looks_like_org(name: str, handle: str) -> bool:
    if (_tokens(name) | _tokens(handle)) & _ORG_TOKENS:
        return True
    h = (handle or "").lower()
    return any(h.endswith(suf) and len(h) > len(suf) + 2 for suf in _ORG_HANDLE_SUFFIX)


def _term_re(terms) -> re.Pattern | None:
    """One regex for a list of words or phrases, matched on word boundaries, case-insensitive."""
    clean = sorted({" ".join(str(t).lower().split()) for t in terms if str(t).strip()}, key=len, reverse=True)
    clean = [t for t in clean if 2 <= len(t) <= 40]
    if not clean:
        return None
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in clean) + r")(?![a-z0-9])", re.I)


@dataclass
class Terms:
    """Compiled word lists for prescore()."""
    builder: re.Pattern | None
    off: re.Pattern | None

    @classmethod
    def build(cls, builder=(), off=()) -> "Terms":
        return cls(_term_re(list(BUILDER_TERMS) + list(builder)), _term_re(list(OFF_TARGET_TERMS) + list(off)))


def _hits(rx: re.Pattern | None, text: str) -> set[str]:
    return {m.lower() for m in rx.findall(text)} if rx and text else set()


def prescore(f: Found, terms: Terms) -> tuple[int, list[str]]:
    """Free relevance estimate from the bio and the posts we already hold.

    A bio is how a person describes themselves, so it counts most: +3 per distinct builder term
    (at most 3 terms) and -4 per distinct off-target term. Posts count less, because anyone can
    post about a topic: +1 per distinct builder term (at most 3) and -1 per off-target term
    (at most 2). An off-target word in the display name or handle is another -4.
    Returns (score, off-target terms that matched).
    """
    u = f.user
    posts = " ".join(p.text for p in f.posts)
    bio_b, bio_o = _hits(terms.builder, u.bio), _hits(terms.off, u.bio)
    post_b, post_o = _hits(terms.builder, posts) - bio_b, _hits(terms.off, posts) - bio_o
    name_o = _hits(terms.off, f"{u.name} {u.handle.replace('_', ' ')}") - bio_o
    score = (3 * min(len(bio_b), 3) + min(len(post_b), 3)
             - 4 * len(bio_o) - 4 * len(name_o) - min(len(post_o), 2))
    return score, sorted(bio_o | name_o | post_o)


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
    if looks_like_org(user.name, user.handle) or _ORG_BIO.search(user.bio):
        return "organisation, news or bot account"
    if need_text and not user.bio.strip() and len(_URL.sub("", post_text).strip()) < 60:
        return "no bio and almost no text"
    return None


# At or below this pre-score (off-target signs outweigh any builder signs by this much), an
# account is left out without paying for a score. The operator sees who and why in the notes.
OFF_TARGET_CUTOFF = -4


def rank_key(f: Found) -> tuple:
    """Higher sorts first: the free pre-score, then (as tie-breaks) people X says we can DM,
    matched by more queries, with more posts, with a bio."""
    return (f.pre, f.user.can_dm is not False, len(f.queries), min(len(f.posts), 3), bool(f.user.bio.strip()))


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


def limits(depth: str) -> dict:
    """Search budget for one campaign. 'wide' trades about three times the cost for more people."""
    s = get_settings()
    k = max(1, s.wide_search_factor) if depth == "wide" else 1
    return {"queries": min(8, s.max_queries * k), "per_query": min(100, s.posts_per_query * k),
            "posts": s.max_posts_per_campaign * k, "candidates": s.max_candidates_per_campaign * k}


def estimate_usd(depth: str) -> float:
    """Upper-bound estimate shown before a search starts."""
    s, lim = get_settings(), limits(depth)
    return lim["posts"] * s.cost_x_post_read + (lim["candidates"] + 1) * s.cost_llm_fast


def _search_segment(segment: str, x: XClient, llm: LLMClient, account_id: int, want: int,
                    skip: set[str], notes: list[str], cost: dict, lim: dict) -> tuple[dict[str, Found], Terms]:
    """Run the LLM's queries within the post budget. `want` is how many scorable people we are aiming for.
    Also returns the word lists the LLM gave for ranking authors."""
    s = get_settings()
    plan = llm.build_queries(segment)
    terms = Terms.build(plan.builder_terms[:16], plan.off_target_terms[:16])
    queries = clean_queries(plan.queries, lim["queries"])
    quota.record(account_id, "llm_fast", 1)
    cost["llm_fast"] += 1
    if not queries:
        raise LLMError("the model returned no search queries")

    found: dict[str, Found] = {}
    seen_posts: set[str] = set()
    left = lim["posts"]
    more: list[tuple[int, str, str]] = []   # (new authors on first page, query, next_token)
    failed: list[str] = []

    def page(q: str, token: str | None) -> tuple[int, str | None]:
        nonlocal left
        # 10 is the smallest page X serves; every returned post is billed.
        posts, users, nxt = x.search_page(q, max(10, min(lim["per_query"], left)), token)
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
    return found, terms


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
    lim = limits(camp["depth"] if "depth" in camp.keys() else "quick")
    budget = lim["candidates"] if left is None else min(left, lim["candidates"])
    skip = known | {camp["me"]}
    notes: list[str] = []
    cost = {"posts": 0, "users": 0, "llm_fast": 0}
    is_segment = camp["mode"] == "segment"

    if is_segment:
        segment = camp["segment"]
        found, terms = _search_segment(segment, x, llm, account_id, budget, skip, notes, cost, lim)
    else:
        segment = camp["segment"] or DEFAULT_SEGMENT
        found = _follow_seeds(camp["seeds"], x, notes, cost)
        terms = Terms.build()

    fresh = [f for k, f in found.items() if k not in skip]
    skipped_known = len(found) - len(fresh)

    kept, dropped = [], []
    for f in fresh:
        why = prefilter_reason(f.user, f.posts, need_text=is_segment)
        if not why:
            f.pre, f.off = prescore(f, terms)
            if f.pre <= OFF_TARGET_CUTOFF and f.off:
                why = "reads as off-target: " + ", ".join(f.off[:3])
        (dropped if why else kept).append((f, why))
    kept = sorted((f for f, _ in kept), key=rank_key, reverse=True)
    if len(kept) > budget:
        notes.append(f"{len(kept) - budget} more people were found but not scored (limit {budget} per run); "
                     "people whose bio and posts show the most signs of doing this work were scored first.")
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
                " can_dm, source, avatar_url, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, campaign_id, user.id, user.handle, user.name, user.bio, user.followers,
                 None if user.can_dm is None else int(user.can_dm), f.source, user.avatar_url, iso()),
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
