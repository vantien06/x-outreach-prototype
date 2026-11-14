"""Discovery: query handling, post budget, cheap pre-filters, ranking, seed evidence."""
import pytest

from app.config import get_settings
from app.db import db
from app.llm.base import Fit, Queries
from app.llm.mock import MockLLM
from app.services import discovery
from app.x.base import XError, XPost, XRateLimited, XUser
from app.x.mock import MockX

from .conftest import make_account


def campaign(account_id: int, mode: str = "segment", segment: str = "robotics builders", seeds: str = "") -> int:
    with db() as conn:
        return conn.execute(
            "INSERT INTO campaigns (account_id, name, mode, segment, seeds, pitch, created_at)"
            " VALUES (?, 'c', ?, ?, ?, 'p', '2026-01-01')", (account_id, mode, segment, seeds)).lastrowid


def camp_row(cid: int):
    with db() as conn:
        return conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()


def candidates() -> dict:
    with db() as conn:
        return {r["handle"]: r for r in conn.execute("SELECT * FROM candidates")}


class ScriptedLLM(MockLLM):
    def __init__(self, queries):
        self._queries = queries
        self.scored: list[str] = []

    def build_queries(self, segment):
        return Queries(queries=self._queries)

    def score_fit(self, segment, person):
        self.scored.append(person.handle)
        return Fit(score=70, category="builder", reason="ok", evidence_ids=[e.id for e in person.evidence][:1])


def user(i: int, bio: str = "Robotics engineer building arms.", **kw) -> XUser:
    return XUser(id=str(1000 + i), handle=f"u{i}", name=f"Person {i}", bio=bio, **kw)


def post(i: int, n: int = 0, text: str = "We shipped a new gripper controller this week, notes inside.") -> XPost:
    return XPost(id=f"{1000 + i}-{n}", author_id=str(1000 + i), text=text, author_handle=f"u{i}")


class SearchX:
    """pages: {query: [page, page, ...]}, each page a list of (user, post). An Exception value is raised."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[tuple[str, str | None, int]] = []

    def search_page(self, query, max_results, next_token=None):
        self.calls.append((query, next_token, max_results))
        spec = self.pages.get(query, [[]])
        if isinstance(spec, Exception):
            raise spec
        idx = int(next_token or 0)
        items = spec[idx][:max_results]
        nxt = str(idx + 1) if idx + 1 < len(spec) else None
        return [p for _, p in items], {u.id: u for u, _ in items}, nxt


Q1, Q2 = "(gripper OR manipulator) -is:retweet", "(#robotics) -is:retweet"


# ---------------------------------------------------------------- queries

def test_clean_queries_dedupes_and_excludes_reposts():
    out = discovery.clean_queries(["  robot   arm ", "robot arm", "", "x -is:retweet", "a", "b", "c", "d"], 5)
    assert out == ["robot arm -is:retweet", "x -is:retweet", "a -is:retweet", "b -is:retweet", "c -is:retweet"]


def test_refused_query_is_reported_and_the_rest_still_run():
    acc = make_account()
    x = SearchX({Q1: XError("400: invalid operator"), Q2: [[(user(1), post(1))]]})
    cid = campaign(acc["id"])
    discovery.run_campaign(cid, x, ScriptedLLM([Q1, Q2]))
    row = camp_row(cid)
    assert row["status"] == "ready"
    assert "Queries X refused" in row["note"] and "invalid operator" in row["note"]
    assert set(candidates()) == {"u1"}


def test_all_queries_refused_fails_with_the_reason():
    acc = make_account()
    cid = campaign(acc["id"])
    discovery.run_campaign(cid, SearchX({Q1: XError("400: bad"), Q2: XError("400: bad")}), ScriptedLLM([Q1, Q2]))
    row = camp_row(cid)
    assert row["status"] == "failed" and "every search query was refused" in row["note"]


def test_rate_limit_fails_the_search_instead_of_being_swallowed():
    acc = make_account()
    cid = campaign(acc["id"])
    discovery.run_campaign(cid, SearchX({Q1: XRateLimited("slow down")}), ScriptedLLM([Q1]))
    assert camp_row(cid)["status"] == "failed"


def test_post_budget_is_never_exceeded(monkeypatch):
    monkeypatch.setenv("MAX_POSTS_PER_CAMPAIGN", "30")
    monkeypatch.setenv("POSTS_PER_QUERY", "20")
    get_settings.cache_clear()
    acc = make_account()
    page = [(user(i), post(i)) for i in range(40)]
    x = SearchX({Q1: [page, page], Q2: [[(user(100 + i), post(100 + i)) for i in range(40)]]})
    cid = campaign(acc["id"])
    discovery.run_campaign(cid, x, ScriptedLLM([Q1, Q2]))
    assert sum(n for _, _, n in x.calls) <= 30
    assert "30 posts read" in camp_row(cid)["note"]


def test_second_page_is_read_only_when_short_of_people(monkeypatch):
    monkeypatch.setenv("MAX_CANDIDATES_PER_CAMPAIGN", "2")
    get_settings.cache_clear()
    acc = make_account()
    few = SearchX({Q1: [[(user(1), post(1))], [(user(2), post(2))]]})
    discovery.run_campaign(campaign(acc["id"]), few, ScriptedLLM([Q1]))
    assert [c[1] for c in few.calls] == [None, "1"]          # short of people: page two was read
    assert set(candidates()) == {"u1", "u2"}

    acc2 = make_account("7002", "other")
    plenty = SearchX({Q1: [[(user(10 + i), post(10 + i)) for i in range(6)], [(user(30), post(30))]]})
    discovery.run_campaign(campaign(acc2["id"]), plenty, ScriptedLLM([Q1]))
    assert [c[1] for c in plenty.calls] == [None]             # enough people already: no second page


# ---------------------------------------------------------------- pre-filter and ranking

@pytest.mark.parametrize("u,posts,need_text,expected", [
    (XUser(id="1", handle="a", name="AI Daily News", bio="All the AI stories."), [], True, "organisation"),
    (XUser(id="1", handle="a", name="Sam", bio="Official account of Acme Robotics."), [], True, "organisation"),
    (XUser(id="1", handle="a", name="Sam", bio="100x gems. DM for promo."), [], True, "promotion"),
    (XUser(id="1", handle="a", name="Sam", bio=""), [post(1, text="https://t.co/abc")], True, "almost no text"),
    (XUser(id="1", handle="a", name="Sam", bio=""), [], False, None),   # seeds: posts are read later
    (XUser(id="1", handle="a", name="Sam", bio="Founder building warehouse robots."), [post(1)], True, None),
    (XUser(id="1", handle="a", name="Sam", bio="I write a newsletter about my robot arm builds."), [], True, None),
])
def test_prefilter(u, posts, need_text, expected):
    reason = discovery.prefilter_reason(u, posts, need_text=need_text)
    assert (reason is None) if expected is None else (reason and expected in reason)


def test_filtered_accounts_are_not_scored_and_are_named_in_the_note():
    acc = make_account()
    cid = campaign(acc["id"], segment="robotics and AI builders")
    discovery.run_campaign(cid, MockX(acc["x_user_id"]), MockLLM())
    found, note = candidates(), camp_row(cid)["note"]
    assert found["demo_linh_robotics"]["fit_score"] >= 60
    assert found["demo_grace_foodie"]["fit_score"] < 60            # a real person, just not a fit: scored low
    assert "demo_tom_cryptoalpha" not in found and "demo_newsbot_ai" not in found
    assert "left out before scoring" in note and "@demo_tom_cryptoalpha" in note
    with db() as conn:
        for r in found.values():
            assert conn.execute("SELECT COUNT(*) FROM evidence WHERE candidate_id=?", (r["id"],)).fetchone()[0] >= 1


def test_reachable_people_are_scored_before_people_with_closed_dms(monkeypatch):
    monkeypatch.setenv("MAX_CANDIDATES_PER_CAMPAIGN", "2")
    get_settings.cache_clear()
    acc = make_account()
    closed = [(user(i, can_dm=False), post(i)) for i in range(3)]
    open_ = [(user(10 + i, can_dm=True), post(10 + i)) for i in range(2)]
    llm = ScriptedLLM([Q1])
    cid = campaign(acc["id"])
    discovery.run_campaign(cid, SearchX({Q1: [closed + open_]}), llm)
    assert set(llm.scored) == {"u10", "u11"}
    assert "more people were found but not scored" in camp_row(cid)["note"]


def test_profile_location_is_kept_as_evidence():
    acc = make_account()
    x = SearchX({Q1: [[(user(1, location="Hanoi, Vietnam"), post(1))]]})
    discovery.run_campaign(campaign(acc["id"]), x, ScriptedLLM([Q1]))
    with db() as conn:
        texts = [r["text"] for r in conn.execute("SELECT text FROM evidence ORDER BY id")]
    assert "Profile location: Hanoi, Vietnam" in texts


def test_note_reports_cost_estimate_when_live(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    acc = make_account()
    cid = campaign(acc["id"])
    discovery.run_campaign(cid, SearchX({Q1: [[(user(i), post(i)) for i in range(10)]]}), ScriptedLLM([Q1]))
    assert "Estimated cost of this search: $0.06 (10 posts read, 10 people scored" in camp_row(cid)["note"]


# ---------------------------------------------------------------- seed campaigns

class SeedX:
    def __init__(self, following, posts=None, errors=None):
        self._following = following
        self._posts = posts or {}
        self._errors = errors or {}
        self.post_reads: list[str] = []

    def lookup_usernames(self, handles):
        return [XUser(id="1", handle=h, name=h) for h in handles]

    def following(self, user_id, max_results):
        return self._following[:max_results]

    def user_posts(self, user, max_results):
        self.post_reads.append(user.handle)
        if user.handle in self._errors:
            raise self._errors[user.handle]
        return self._posts.get(user.handle, [])[:max_results]


def test_seed_people_get_posts_before_scoring():
    acc = make_account()
    x = SeedX([user(1), user(2, bio="")], posts={"u1": [post(1)], "u2": [post(2, text="Our sim-to-real transfer finally works on the arm.")]})
    llm = ScriptedLLM([])
    discovery.run_campaign(campaign(acc["id"], mode="seeds", seeds="@seed"), x, llm)
    assert sorted(x.post_reads) == ["u1", "u2"] and sorted(llm.scored) == ["u1", "u2"]
    with db() as conn:
        for handle in ("u1", "u2"):
            n = conn.execute(
                "SELECT COUNT(*) FROM evidence e JOIN candidates c ON c.id=e.candidate_id"
                " WHERE c.handle=? AND e.kind='post'", (handle,)).fetchone()[0]
            assert n == 1


def test_seed_person_with_nothing_to_judge_is_not_scored_and_says_why():
    acc = make_account()
    x = SeedX([user(1, bio=""), user(2, bio="")], errors={"u2": XError("401: not authorized")})
    llm = ScriptedLLM([])
    discovery.run_campaign(campaign(acc["id"], mode="seeds", seeds="@seed"), x, llm)
    found = candidates()
    assert llm.scored == []
    assert found["u1"]["fit_score"] is None and "no bio" in found["u1"]["fit_reason"]
    assert "no recent original posts" in found["u1"]["fit_reason"]
    assert "401: not authorized" in found["u2"]["fit_reason"]


def test_seed_person_with_bio_but_unreadable_posts_is_scored_with_a_note():
    acc = make_account()
    x = SeedX([user(1)], errors={"u1": XError("403: protected")})
    discovery.run_campaign(campaign(acc["id"], mode="seeds", seeds="@seed"), x, ScriptedLLM([]))
    row = candidates()["u1"]
    assert row["fit_score"] == 70 and "403: protected" in row["fit_reason"]


def test_rate_limit_on_post_reads_stops_reading_but_not_the_campaign():
    acc = make_account()
    x = SeedX([user(1), user(2), user(3)], errors={"u1": XRateLimited("slow"), "u2": XRateLimited("slow")})
    cid = campaign(acc["id"], mode="seeds", seeds="@seed")
    discovery.run_campaign(cid, x, ScriptedLLM([]))
    assert camp_row(cid)["status"] == "ready"
    assert len(x.post_reads) == 1                       # stopped after the first 429
    assert all("rate-limited" in r["fit_reason"] for r in candidates().values())
