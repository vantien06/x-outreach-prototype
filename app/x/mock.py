"""Dry-run X. Every person here is FICTIONAL; handles, bios and posts are invented.

The mock keeps a simulated DM inbox in SQLite so that a "sent" message shows up
in DM history and some personas reply a few seconds later.

It mirrors what the live API showed on 2026-11-14: replies are only visible in
the chat history (`chat_events`), not on the legacy DM endpoints, and a reply
typed in the X app is end-to-end encrypted. Personas marked "encrypted" reply
that way (preceded by a key change and an "accepted the request" event); the
others reply in readable text so the reply labels can be shown.
"""
from __future__ import annotations

from datetime import timedelta

from ..db import db, iso, now
from .base import ChatEvent, DMEvent, SendResult, XAmbiguous, XForbidden, XPost, XUser

REPLY_DELAY_SECONDS = 4

# dm: open | closed (403) | timeout (ambiguous failure) ; prior: already has a DM thread
PERSONAS: list[dict] = [
    {
        "id": "9001", "handle": "demo_linh_robotics", "name": "Linh Tran (demo)", "followers": 4200,
        "bio": "Founder @ PalletPilot (fictional). Building warehouse picking robots in Ho Chi Minh City. Ex-controls engineer.",
        "posts": [
            "We finally got our gripper to pick 600 mixed SKUs/hour without retraining per SKU. The trick was tactile feedback, not a bigger vision model.",
            "Hiring a robotics perception engineer in HCMC. Sim-to-real experience matters more than paper count.",
        ],
        "reply": "Thanks for reaching out! Happy to chat. What does the fund usually look for at pre-seed?",
    },
    {
        "id": "9002", "handle": "demo_arjun_infer", "name": "Arjun Mehta (demo)", "followers": 11800,
        "bio": "Building tinyserve (fictional): open-source LLM inference server for single-GPU boxes. Singapore.",
        "posts": [
            "tinyserve 0.4 is out: continuous batching + KV cache reuse, 2.3x throughput on a single 24GB card in our benchmark.",
            "Hot take: most teams in SEA don't need a cluster, they need one well-used GPU and good batching.",
        ],
        "reply": "Not looking for funding right now, but thanks.",
    },
    {
        "id": "9003", "handle": "demo_siti_embodied", "name": "Siti Rahma (demo)", "followers": 2900,
        "bio": "PhD researcher, embodied AI and manipulation. Working on language-conditioned policies for low-cost arms. Jakarta.",
        "posts": [
            "New preprint: a language-conditioned policy trained on 40 hours of teleop data that transfers to a $300 arm.",
            "Teleoperation data quality > quantity. We threw away 30% of our demos and success rate went up.",
        ],
        "reply": "Interesting. Can you send more details about the programme?", "encrypted": True,
    },
    {
        "id": "9004", "handle": "demo_kenji_agents", "name": "Kenji Sato (demo)", "followers": 7600,
        "bio": "Co-founder, Loopwork (fictional). Agent tooling for back-office ops. Previously ML infra.",
        "posts": [
            "Our agent eval harness now replays 1,200 real support tickets nightly. Evals are the product.",
            "Shipping tool-use agents to finance teams taught me: audit logs first, autonomy second.",
        ],
        "dm": "closed",
    },
    {
        "id": "9005", "handle": "demo_mai_speech", "name": "Mai Nguyen (demo)", "followers": 3300,
        "bio": "ML engineer. Vietnamese speech recognition and TTS models. Open-sourcing a 200h dataset soon.",
        "posts": [
            "Fine-tuned a small ASR model on 200 hours of Vietnamese call-centre audio. WER dropped from 18% to 9%.",
            "Tonal languages break a lot of off-the-shelf TTS. Sharing our failure cases next week.",
        ],
        "dm": "timeout",
    },
    {
        "id": "9006", "handle": "demo_ravi_drones", "name": "Ravi Kumar (demo)", "followers": 5100,
        "bio": "CTO at SkyFurrow (fictional): autonomous crop-spraying drones for smallholder farms. Bangkok.",
        "posts": [
            "Our drones flew 3,000 hectares this season with onboard vision only, no RTK base station.",
            "Edge inference on a 15W module is a different sport from cloud ML.",
        ],
        "reply": "Please don't contact me again.",
    },
    {
        "id": "9007", "handle": "demo_ana_devtools", "name": "Ana Reyes (demo)", "followers": 6400,
        "bio": "Building promptdiff (fictional): regression testing for LLM apps. Manila. Developer tools nerd.",
        "posts": [
            "promptdiff now catches silent quality regressions when you swap model versions. 40 teams in the beta.",
            "Every LLM app needs a golden set. Start with 50 examples, not 5,000.",
        ],
        "prior": True,
    },
    {
        "id": "9008", "handle": "demo_wei_vision", "name": "Wei Chen (demo)", "followers": 1500,
        "bio": "Research engineer, 3D vision for robot navigation. Kuala Lumpur.",
        "posts": [
            "Got a gaussian-splat map running in real time on a mobile robot for indoor navigation.",
        ],
        "reply": None,
    },
    {
        "id": "9009", "handle": "demo_farah_health", "name": "Farah Aziz (demo)", "followers": 2100,
        "bio": "Founder, ClinicNote (fictional). AI scribe for clinics in Malaysia and Indonesia.",
        "posts": [
            "Our AI scribe now handles code-switching between Malay and English in the same consult.",
        ],
        "reply": None,
    },
    {
        "id": "9010", "handle": "demo_tom_cryptoalpha", "name": "Tom (demo)", "followers": 48000,
        "bio": "100x gems daily. Trading signals. DM for promo. Not financial advice.",
        "posts": ["AI coins are about to explode. Thread on my top 5 picks."],
    },
    {
        "id": "9011", "handle": "demo_grace_foodie", "name": "Grace Lim (demo)", "followers": 900,
        "bio": "Food blogger. Hawker centres, noodles and coffee across Southeast Asia.",
        "posts": ["Tried the robot barista at the new mall. The coffee was fine, the queue was not."],
    },
    {
        "id": "9012", "handle": "demo_newsbot_ai", "name": "AI Headlines (demo)", "followers": 22000,
        "bio": "Automated feed of AI news headlines. Bot account.",
        "posts": ["BREAKING: new model released. Read more at the link."],
    },
]

_BY_ID = {p["id"]: p for p in PERSONAS}
_BY_HANDLE = {p["handle"].lower(): p for p in PERSONAS}
SEED = {"id": "8001", "handle": "demo_seed_lab", "name": "Demo Seed Lab", "bio": "Fictional seed account."}
ME = XUser(id="7001", handle="demo_sender", name="Demo Sender", bio="Dry-run account. Nothing is sent to X.")


def _user(p: dict) -> XUser:
    return XUser(
        id=p["id"], handle=p["handle"], name=p["name"], bio=p["bio"], followers=p.get("followers", 0),
        can_dm=False if p.get("dm") == "closed" else True,
    )


def _posts(p: dict) -> list[XPost]:
    base = now() - timedelta(days=1)
    return [
        XPost(id=f"{p['id']}{i:02d}", author_id=p["id"], text=t, author_handle=p["handle"],
              created_at=iso(base - timedelta(hours=7 * i)))
        for i, t in enumerate(p["posts"])
    ]


class MockX:
    def __init__(self, owner_x_user_id: str = ME.id):
        self.owner = owner_x_user_id

    def me(self) -> XUser:
        return ME

    def search_recent(self, query: str, max_results: int):
        posts, users = [], {}
        for p in PERSONAS:
            users[p["id"]] = _user(p)
            posts.extend(_posts(p))
        return posts[:max_results], users

    def lookup_usernames(self, handles: list[str]) -> list[XUser]:
        out = []
        for h in handles:
            p = _BY_HANDLE.get(h.lower())
            out.append(_user(p) if p else XUser(id=SEED["id"], handle=h, name=SEED["name"], bio=SEED["bio"]))
        return out

    def following(self, user_id: str, max_results: int) -> list[XUser]:
        return [_user(p) for p in PERSONAS[:9]][:max_results]

    def user_posts(self, user: XUser, max_results: int) -> list[XPost]:
        p = _BY_ID.get(user.id)
        return _posts(p)[:max_results] if p else []

    def send_dm(self, participant_id: str, text: str) -> SendResult:
        p = _BY_ID.get(participant_id, {})
        if p.get("dm") == "closed":
            raise XForbidden("You cannot send messages to this user (they do not accept DMs from this account).")
        if p.get("dm") == "timeout":
            raise XAmbiguous("Simulated network timeout while sending")
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO mock_dm_events (owner_x_user_id, participant_id, sender_id, text, visible_at)"
                " VALUES (?,?,?,?,?)",
                (self.owner, participant_id, self.owner, text, iso()),
            )
            event_id = f"mock-{cur.lastrowid}"
            if p.get("reply"):
                at = iso(now() + timedelta(seconds=REPLY_DELAY_SECONDS))
                # An encrypted reply arrives as three events and its text is not readable.
                incoming = ([("system", ""), ("activity", ""), ("encrypted", "")] if p.get("encrypted")
                            else [("message", p["reply"])])
                for kind, body in incoming:
                    conn.execute(
                        "INSERT INTO mock_dm_events (owner_x_user_id, participant_id, sender_id, text, visible_at, kind)"
                        " VALUES (?,?,?,?,?,?)",
                        (self.owner, participant_id, participant_id, body, at, kind),
                    )
        return SendResult(event_id=event_id, conversation_id=f"{participant_id}-{self.owner}")

    def _events(self, where: str, args: tuple) -> list[DMEvent]:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM mock_dm_events WHERE owner_x_user_id=? AND visible_at<=? AND kind='message'"
                f" AND sender_id=owner_x_user_id {where} ORDER BY id DESC",
                (self.owner, iso(), *args),
            ).fetchall()
        return [
            DMEvent(id=f"mock-{r['id']}", conversation_id=f"{r['participant_id']}-{self.owner}",
                    sender_id=r["sender_id"], text=r["text"], created_at=r["visible_at"])
            for r in rows
        ]

    def dm_events(self, max_results: int = 100) -> list[DMEvent]:
        return self._events("", ())[:max_results]

    def conversation_events(self, participant_id: str) -> list[DMEvent]:
        events = self._events("AND participant_id=?", (participant_id,))
        if _BY_ID.get(participant_id, {}).get("prior"):
            events.append(DMEvent(id=f"prior-{participant_id}", conversation_id=f"{participant_id}-{self.owner}",
                                  sender_id=self.owner, text="(an earlier manual DM)",
                                  created_at=iso(now() - timedelta(days=20))))
        return events

    def chat_events(self, conversation_id: str) -> list[ChatEvent]:
        participant = next((part for part in conversation_id.split("-") if part != self.owner), "")
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM mock_dm_events WHERE owner_x_user_id=? AND participant_id=? AND visible_at<=?"
                " ORDER BY id DESC", (self.owner, participant, iso()),
            ).fetchall()
        return [
            ChatEvent(id=f"mock-{r['id']}", conversation_id=conversation_id, sender_id=r["sender_id"],
                      created_at=r["visible_at"], kind=r["kind"], text=r["text"])
            for r in rows
        ]
