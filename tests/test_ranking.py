"""Free pre-score and organisation filter: who gets paid-for LLM scoring, and who is left out."""
import pytest

from app.config import get_settings
from app.llm.base import Queries
from app.services import discovery
from app.services.discovery import Found, Terms, looks_like_org, prescore
from app.x.base import XPost, XUser

from .conftest import make_account
from .test_discovery import Q1, ScriptedLLM, SearchX, camp_row, campaign, candidates, post, user


# ---------------------------------------------------------------- organisation / brand / bot names

@pytest.mark.parametrize("name,handle", [
    ("ZOOMEX_Official", "ZOOMEX_Official"),      # underscore-joined
    ("Zoomex", "zoomexofficial"),                # run together in the handle
    ("AgentsDaily", "agentsdaily"),              # camelCase in the name, suffix in the handle
    ("Orchid", "orchid_hq"),
    ("LangFlow HQ", "langflow"),
    ("AI News", "ainews"),
    ("Robotics Jobs", "robojobs"),
    ("SEA Tech Summit 2026", "seatechsummit"),
    ("PaperBot", "paper_bot"),
])
def test_org_names_are_caught(name, handle):
    assert looks_like_org(name, handle)


@pytest.mark.parametrize("name,handle", [
    ("Linh Tran", "linh_robotics"),              # "robotics" must not match "bot"
    ("Sam Abbot", "samabbot"),                   # surname ending in "bot"
    ("Dana Newsome", "dnewsome"),                # surname starting with "news"
    ("Matthew Shq", "mshq"),                     # too short for the "hq" ending rule
    ("Priya | building agents", "priya_builds"),
    ("Chris Daly", "chrisdaly"),
    ("Mediana Putri", "mediana_p"),              # "media" only as part of a longer word
    ("Tran Nguyen", "trannguyen"),
])
def test_real_people_are_not_caught(name, handle):
    assert not looks_like_org(name, handle)


# ---------------------------------------------------------------- pre-score

def found(bio="", posts=(), name="Sam", handle="sam") -> Found:
    u = XUser(id="1", handle=handle, name=name, bio=bio)
    return Found(user=u, source="search: q",
                 posts=[XPost(id=str(i), author_id="1", text=t, author_handle=handle) for i, t in enumerate(posts)])


TERMS = Terms.build(["langgraph", "ai agents", "rag pipeline"], ["ai news", "prompt pack"])


def test_builder_bio_outranks_topic_only_bio():
    builder = found("Founder building AI agents for logistics with LangGraph.")
    curator = found("I share the latest AI news and curated threads every day.")
    wellness = found("A girl living in the U.S. who loves wellness.", ["AI agents are everywhere now!"])
    b, c, w = (prescore(f, TERMS)[0] for f in (builder, curator, wellness))
    assert b > 0 > c and b > w
    assert c <= discovery.OFF_TARGET_CUTOFF          # the curator is left out without a paid score


def test_bio_counts_more_than_posts():
    in_bio = found("Research engineer working on a RAG pipeline.")
    in_posts = found("Dad. Coffee.", ["Our RAG pipeline went live today, we shipped it after two rewrites."])
    assert prescore(in_bio, TERMS)[0] > prescore(in_posts, TERMS)[0] > 0


def test_terms_match_whole_words_only():
    # "phd" must not match inside another word, "news" must not match "newsome", "ai agents" needs both words.
    f = found("Aphdx fan. Friend of Dana Newsome. I like agents in spy films.")
    assert prescore(f, TERMS) == (0, [])


def test_off_target_word_in_the_name_counts():
    f = found("Sharing things I like.", name="Crypto Trading Signals", handle="alpha_trading")
    score, off = prescore(f, TERMS)
    assert score <= discovery.OFF_TARGET_CUTOFF and "trading" in off


def test_a_builder_who_mentions_an_off_target_word_is_kept():
    f = found("Co-founder and CTO. Building AI agents. Speaker at the PyCon conference.")
    assert prescore(f, TERMS)[0] > discovery.OFF_TARGET_CUTOFF


def test_llm_word_lists_are_optional():
    # An older or minimal LLM answer has no lists: the built-in ones still apply.
    assert Queries(queries=["q"]).builder_terms == []
    t = Terms.build()
    assert prescore(found("Founder building robots."), t)[0] > 0


# ---------------------------------------------------------------- effect on a campaign

class TermsLLM(ScriptedLLM):
    def build_queries(self, segment):
        return Queries(queries=self._queries, builder_terms=["ai agents", "langgraph"], off_target_terms=["ai news"])


def test_scoring_budget_goes_to_builders_first(monkeypatch):
    """Six authors, budget for three: the three who describe doing the work are scored, whatever
    order X returned them in and even though one of them has DMs closed."""
    monkeypatch.setattr(get_settings(), "max_candidates_per_campaign", 3)
    people = [
        (user(1, bio="Tech enthusiast. Opinions my own."), post(1, text="AI agents will change everything, thread below.")),
        (user(2, bio="Founder building AI agents for clinics."), post(2)),
        (user(3, bio="Coffee and football."), post(3, text="Tried one of those AI agents today, it was fine I guess.")),
        (user(4, bio="Engineer. LangGraph, evals, tooling.", can_dm=False), post(4)),
        (user(5, bio="Marketing lead at a retail company."), post(5, text="AI agents for marketing: what do you all think?")),
        (user(6, bio="PhD student, multi-agent planning."), post(6, text="We shipped our AI agents benchmark today.")),
    ]
    acc = make_account()
    cid = campaign(acc["id"], segment="people building AI agents")
    llm = TermsLLM([Q1])
    discovery.run_campaign(cid, SearchX({Q1: [people]}), llm)
    assert sorted(llm.scored) == ["u2", "u4", "u6"]
    assert "3 more people matched the search but were not reviewed" in camp_row(cid)["note"]


def test_off_target_accounts_are_left_out_and_named(monkeypatch):
    people = [
        (user(1, bio="Founder building AI agents for clinics."), post(1)),
        (XUser(id="2002", handle="ZOOMEX_Official", name="ZOOMEX_Official", bio="Trade smarter."),
         XPost(id="p2", author_id="2002", text="AI agents are the future of trading. Join now.", author_handle="ZOOMEX_Official")),
        (user(3, bio="Your daily AI news and curated threads."), post(3, text="AI agents roundup: ten launches this week.")),
    ]
    acc = make_account()
    cid = campaign(acc["id"], segment="people building AI agents")
    llm = TermsLLM([Q1])
    discovery.run_campaign(cid, SearchX({Q1: [people]}), llm)
    note = camp_row(cid)["note"]
    assert llm.scored == ["u1"] and set(candidates()) == {"u1"}
    assert "2 accounts were skipped" in note
    assert "@ZOOMEX_Official (organisation, news or bot account)" in note
    assert "@u3 (reads as off-target:" in note
