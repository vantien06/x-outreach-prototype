from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import clients
from .config import get_settings
from .db import db, init_db, iso, seconds_ago
from .llm.base import LLMError
from .services import accounts, discovery, outreach, quota
from .worker import Worker
from .x.base import XAmbiguous, XError, chat_conversation_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s or "[]")
templates.env.filters["short"] = lambda s: (s or "")[:16].replace("T", " ")


def chat_url(row, account) -> str:
    """Link that opens this 1:1 conversation on X (where encrypted replies can be read)."""
    conv = row["dm_conversation_id"] or chat_conversation_id(account["x_user_id"], row["recipient_x_user_id"])
    return f"https://x.com/i/chat/{conv}"


templates.env.globals["chat_url"] = chat_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    init_db()
    with db() as conn:
        # Drafting runs in this process; a restart mid-draft must not leave people stuck as "drafting".
        conn.execute("UPDATE candidates SET status='new' WHERE status='drafting'")
    for problem in s.config_problems():
        log.warning("configuration: %s", problem)
    worker = Worker() if s.worker_enabled else None
    if worker:
        worker.start()
    yield
    if worker:
        worker.stop()


app = FastAPI(title="Warm Outreach", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware, secret_key=get_settings().session_secret(), same_site="lax",
    https_only=get_settings().base_url.startswith("https://"), max_age=14 * 24 * 3600,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


# ------------------------------------------------------------------ helpers

def current(request: Request):
    return accounts.get(request.session.get("account_id"))


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def nav_data(account) -> dict:
    """The few numbers the sidebar shows on every page."""
    aid = account["id"]
    with db() as conn:
        s = get_settings()
        sends = quota.sends_today(conn, aid)
        return {
            "sends_today": sends, "send_cap": s.daily_send_cap, "send_left": max(0, s.daily_send_cap - sends),
            "locked": quota.budget_locked(conn),
            "drafted": conn.execute(
                "SELECT COUNT(*) FROM outreach WHERE account_id=? AND state='drafted'", (aid,)).fetchone()[0],
            "attention": conn.execute(
                "SELECT (SELECT COUNT(*) FROM dm_events WHERE account_id=? AND direction='in' AND handled=0)"
                " + (SELECT COUNT(*) FROM outreach WHERE account_id=? AND state='needs_check')", (aid, aid)).fetchone()[0],
        }


def render(request: Request, name: str, account=None, **ctx) -> HTMLResponse:
    s = get_settings()
    return templates.TemplateResponse(request, name, {
        "s": s, "account": account, "flashes": request.session.pop("flash", []),
        "problems": s.config_problems(), "nav": nav_data(account) if account else None, **ctx,
    })


def back(request: Request, default: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/", status_code=303)


def review_card(request: Request, account, outreach_id: int, message: str = "") -> HTMLResponse:
    with db() as conn:
        card = load_cards(conn, account["id"], "o.id=?", (outreach_id,))
    return templates.TemplateResponse(request, "_review_card.html", {"c": card[0], "message": message, "s": get_settings()})


def load_cards(conn, account_id: int, where: str, args: tuple = ()) -> list[dict]:
    rows = conn.execute(
        "SELECT o.*, c.name, c.bio, c.followers, c.can_dm, c.fit_score, c.fit_category, c.fit_reason, c.source,"
        " k.name AS campaign_name FROM outreach o JOIN candidates c ON c.id=o.candidate_id"
        f" JOIN campaigns k ON k.id=o.campaign_id WHERE o.account_id=? AND {where} ORDER BY o.created_at, o.id",
        (account_id, *args),
    ).fetchall()
    cards = []
    for r in rows:
        ev = conn.execute("SELECT * FROM evidence WHERE candidate_id=? ORDER BY kind, id", (r["candidate_id"],)).fetchall()
        cards.append({**dict(r), "evidence": [dict(e) for e in ev], "claim_list": json.loads(r["claims"] or "[]")})
    return cards


OUTREACH_WITH_NAME = ("SELECT o.*, c.name AS recipient_name FROM outreach o"
                      " LEFT JOIN candidates c ON c.id=o.candidate_id")
# A reply is anything the recipient sent that is not mere activity (see services/replies.py).
IS_REPLY = "COALESCE(e.label,'') NOT IN ('activity','unknown')"


def dashboard_data(account) -> dict:
    aid = account["id"]
    with db() as conn:
        counts = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) n FROM outreach WHERE account_id=? GROUP BY state", (aid,))}
        replies_open = conn.execute(
            "SELECT e.*, o.recipient_handle, o.recipient_x_user_id, o.dm_conversation_id, o.text AS our_text,"
            " o.sent_at AS our_sent_at,"
            " c.name AS recipient_name FROM dm_events e JOIN outreach o ON o.id=e.outreach_id"
            " LEFT JOIN candidates c ON c.id=o.candidate_id"
            " WHERE e.account_id=? AND e.direction='in' AND e.handled=0 ORDER BY e.id DESC", (aid,)).fetchall()
        replied = conn.execute(
            f"SELECT COUNT(DISTINCT e.outreach_id) FROM dm_events e WHERE e.account_id=? AND e.direction='in' AND {IS_REPLY}",
            (aid,)).fetchone()[0]
        latest = ("(SELECT e.{col} FROM dm_events e WHERE e.outreach_id=o.id AND e.direction='in'"
                  f" ORDER BY {IS_REPLY} DESC, e.id DESC LIMIT 1)")
        sent = conn.execute(
            f"SELECT o.*, c.name AS recipient_name, {latest.format(col='label')} AS reply_label,"
            f" {latest.format(col='text')} AS reply_text, {latest.format(col='id')} AS reply_event"
            " FROM outreach o LEFT JOIN candidates c ON c.id=o.candidate_id"
            " WHERE o.account_id=? AND o.state='sent' ORDER BY o.sent_at DESC LIMIT 100", (aid,)).fetchall()
        by_state = lambda st: conn.execute(
            f"{OUTREACH_WITH_NAME} WHERE o.account_id=? AND o.state=? ORDER BY o.updated_at DESC", (aid, st)).fetchall()
        events: dict[int, list] = {}
        for e in conn.execute(
                "SELECT e.* FROM dm_events e WHERE e.account_id=? AND e.direction='in' AND e.outreach_id IS NOT NULL"
                # Same-second ties: "opened the conversation" reads before the reply it preceded.
                " ORDER BY COALESCE(e.event_at, e.created_at), COALESCE(e.label,'') NOT IN ('activity','unknown'), e.id",
                (aid,)):
            events.setdefault(e["outreach_id"], []).append(e)
        data = {
            "events": events,
            "counts": counts, "replied": replied, "replies_open": replies_open, "sent": sent,
            "needs_check": by_state("needs_check"), "blocked": by_state("blocked"),
            "queue": by_state("approved") + by_state("sending"),
            "campaigns": campaign_list(conn, aid),
            "usage": quota.summary(conn, aid),
        }
    data["attention"] = len(data["replies_open"]) + len(data["needs_check"])
    return data


def campaign_list(conn, account_id: int) -> list:
    return conn.execute(
        "SELECT k.*, (SELECT COUNT(*) FROM candidates c WHERE c.campaign_id=k.id) AS n,"
        " (SELECT COUNT(*) FROM outreach o WHERE o.campaign_id=k.id AND o.state='drafted') AS n_drafted,"
        " (SELECT COUNT(*) FROM outreach o WHERE o.campaign_id=k.id AND o.state='sent') AS n_sent,"
        " (SELECT COUNT(DISTINCT e.outreach_id) FROM dm_events e JOIN outreach o ON o.id=e.outreach_id"
        f"   WHERE o.campaign_id=k.id AND e.direction='in' AND {IS_REPLY}) AS n_replied"
        " FROM campaigns k WHERE k.account_id=? ORDER BY k.id DESC", (account_id,)).fetchall()


# ------------------------------------------------------------------ auth

@app.get("/healthz")
def healthz():
    return {"ok": True, "mode": get_settings().app_mode, "dry_run": get_settings().dry_run}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    account = current(request)
    if not account:
        return render(request, "login.html")
    return render(request, "dashboard.html", account, d=dashboard_data(account))


@app.get("/partials/dashboard", response_class=HTMLResponse)
def dashboard_partial(request: Request):
    account = current(request)
    if not account:
        return HTMLResponse("", status_code=401)
    return templates.TemplateResponse(request, "_dashboard_body.html",
                                      {"d": dashboard_data(account), "account": account, "s": get_settings()})


@app.get("/partials/side", response_class=HTMLResponse)
def side_partial(request: Request):
    account = current(request)
    if not account:
        return HTMLResponse("", status_code=401)
    return templates.TemplateResponse(request, "_side_live.html", {"nav": nav_data(account)})


@app.get("/auth/login")
def auth_login(request: Request):
    s = get_settings()
    if s.dry_run:
        from .x.mock import ME

        request.session["account_id"] = accounts.upsert(ME)
        return login_redirect()
    if not s.x_client_id:
        flash(request, "X_CLIENT_ID is not configured, so sign-in with X cannot start.", "error")
        return login_redirect()
    from .x import real

    state, verifier, challenge = real.new_pkce()
    with db() as conn:
        conn.execute("DELETE FROM oauth_states WHERE created_at<?", (seconds_ago(900),))
        conn.execute("INSERT INTO oauth_states (state, code_verifier, created_at) VALUES (?,?,?)", (state, verifier, iso()))
    return RedirectResponse(real.authorize_url(s.x_client_id, s.redirect_uri, state, challenge), status_code=303)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    s = get_settings()
    if s.dry_run:
        return login_redirect()
    from .x import real

    if error or not code:
        flash(request, f"X sign-in was not completed ({error or 'no code returned'}).", "error")
        return login_redirect()
    with db() as conn:
        row = conn.execute("SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone()
        conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if row is None:
        flash(request, "Sign-in session expired or was already used. Please try again.", "error")
        return login_redirect()
    try:
        token = real.exchange_code(s.x_client_id, s.x_client_secret, code, row["code_verifier"], s.redirect_uri)
        me = real.RealX(lambda: token["access_token"]).me()
        accounts.check_allowed(me.handle)
    except accounts.NotAllowed as e:
        real.revoke(s.x_client_id, s.x_client_secret, token["access_token"])
        flash(request, str(e), "error")
        return login_redirect()
    except (XError, XAmbiguous) as e:
        flash(request, f"X sign-in failed: {e}", "error")
        return login_redirect()
    account_id = accounts.upsert(me)
    clients.store_tokens(account_id, token)
    request.session["account_id"] = account_id
    return login_redirect()


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return login_redirect()


# ------------------------------------------------------------------ campaigns

@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request):
    account = current(request)
    if not account:
        return login_redirect()
    with db() as conn:
        campaigns = campaign_list(conn, account["id"])
    return render(request, "campaigns.html", account, campaigns=campaigns)


@app.post("/campaigns")
def create_campaign(request: Request, background: BackgroundTasks, name: str = Form(""), mode: str = Form("segment"),
                    segment: str = Form(""), seeds: str = Form(""), pitch: str = Form("")):
    account = current(request)
    if not account:
        return login_redirect()
    mode = "seeds" if mode == "seeds" else "segment"
    if mode == "segment" and not segment.strip():
        flash(request, "Describe the target segment.", "error")
        return back(request)
    if mode == "seeds" and not discovery.parse_handles(seeds):
        flash(request, "Enter at least one seed handle, e.g. @someone.", "error")
        return back(request)
    if not pitch.strip():
        flash(request, "Say who you are and why you are reaching out. Drafts are built from this.", "error")
        return back(request)
    with db() as conn:
        try:
            quota.check_budget(conn)
        except quota.LimitReached as e:
            flash(request, str(e), "error")
            return back(request)
        cur = conn.execute(
            "INSERT INTO campaigns (account_id, name, mode, segment, seeds, pitch, created_at) VALUES (?,?,?,?,?,?,?)",
            (account["id"], name.strip() or (segment.strip()[:40] or "Seed campaign"), mode, segment.strip(),
             seeds.strip(), pitch.strip(), iso()),
        )
        campaign_id = cur.lastrowid
    background.add_task(discovery.run_campaign, campaign_id, clients.get_x(account), clients.get_llm())
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


def campaign_ctx(account, campaign_id: int) -> dict | None:
    with db() as conn:
        camp = conn.execute("SELECT * FROM campaigns WHERE id=? AND account_id=?", (campaign_id, account["id"])).fetchone()
        if camp is None:
            return None
        cands = conn.execute(
            "SELECT c.*, o.state AS outreach_state FROM candidates c LEFT JOIN outreach o"
            " ON o.account_id=c.account_id AND o.recipient_x_user_id=c.x_user_id"
            " WHERE c.campaign_id=? ORDER BY c.fit_score IS NULL, c.fit_score DESC, c.id", (campaign_id,)).fetchall()
        out = []
        for c in cands:
            ev = conn.execute("SELECT * FROM evidence WHERE candidate_id=? ORDER BY kind, id", (c["id"],)).fetchall()
            cited = set(json.loads(c["fit_evidence_ids"] or "[]"))
            out.append({**dict(c), "evidence": [{**dict(e), "cited": f"E{e['id']}" in cited} for e in ev]})
    return {"camp": camp, "cands": out}


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_page(request: Request, campaign_id: int):
    account = current(request)
    if not account:
        return login_redirect()
    ctx = campaign_ctx(account, campaign_id)
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    name = "_campaign_body.html" if request.headers.get("hx-request") else "campaign.html"
    return render(request, name, account, **ctx)


def _draft_many(account, candidate_ids: list[int]) -> None:
    x, llm = clients.get_x(account), clients.get_llm()
    for cid in candidate_ids:
        try:
            outreach.create_draft(account, cid, llm, x)
            with db() as conn:
                conn.execute("UPDATE candidates SET status='drafted' WHERE id=? AND status='drafting'", (cid,))
        except (LLMError, quota.LimitReached) as e:
            log.warning("draft failed for candidate %s: %s", cid, e)
            with db() as conn:
                conn.execute("UPDATE candidates SET status='draft_failed', fit_reason=COALESCE(fit_reason,'') || ?"
                             " WHERE id=?", (f" [Draft failed: {e}]", cid))
            if isinstance(e, quota.LimitReached):
                with db() as conn:   # release everyone still waiting in this batch
                    conn.executemany("UPDATE candidates SET status='new' WHERE id=? AND status='drafting'",
                                     [(i,) for i in candidate_ids])
                return
        except Exception as e:
            log.exception("draft failed for candidate %s", cid)
            with db() as conn:
                conn.execute("UPDATE candidates SET status='draft_failed', fit_reason=COALESCE(fit_reason,'') || ?"
                             " WHERE id=?", (f" [Draft failed: {type(e).__name__}]", cid))


@app.post("/campaigns/{campaign_id}/draft")
def draft_selected(request: Request, background: BackgroundTasks, campaign_id: int, candidate_id: list[int] = Form([])):
    account = current(request)
    if not account:
        return login_redirect()
    with db() as conn:
        owned = [r["id"] for r in conn.execute(
            "SELECT id FROM candidates WHERE campaign_id=? AND account_id=? AND status IN ('new','draft_failed')",
            (campaign_id, account["id"]))]
    ids = [c for c in candidate_id if c in owned]
    if not ids:
        flash(request, "Select at least one person to draft a message for.", "error")
        return back(request)
    with db() as conn:
        # Marked before the background job starts, so the review page knows drafts are on their way.
        conn.executemany("UPDATE candidates SET status='drafting' WHERE id=?", [(i,) for i in ids])
    background.add_task(_draft_many, account, ids)
    return RedirectResponse("/review", status_code=303)


# ------------------------------------------------------------------ review

@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request):
    account = current(request)
    if not account:
        return login_redirect()
    with db() as conn:
        cards = load_cards(conn, account["id"], "o.state='drafted'")
        status = review_status(conn, account["id"])
    return render(request, "review.html", account, cards=cards, total=len(cards), **status)


def review_status(conn, account_id: int) -> dict:
    """`drafting`: drafts being written right now. `undrafted`: good fits X lets us message that have no draft."""
    drafting = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE account_id=? AND status='drafting'", (account_id,)).fetchone()[0]
    undrafted = conn.execute(
        "SELECT COUNT(*) FROM candidates c WHERE c.account_id=? AND c.status='new' AND c.fit_score>=?"
        " AND COALESCE(c.can_dm, 1)!=0"
        " AND NOT EXISTS (SELECT 1 FROM outreach o WHERE o.account_id=c.account_id"
        "                 AND o.recipient_x_user_id=c.x_user_id)",
        (account_id, get_settings().fit_threshold)).fetchone()[0]
    return {"drafting": drafting, "undrafted": undrafted}


@app.get("/partials/review", response_class=HTMLResponse)
def review_partial(request: Request, have: str = ""):
    """Polled while drafts are being written: returns the status block plus any cards the page does not show yet."""
    account = current(request)
    if not account:
        return HTMLResponse("", status_code=401)
    shown = {int(v) for v in have.split(",") if v.strip().isdigit()}
    with db() as conn:
        cards = load_cards(conn, account["id"], "o.state='drafted'")
        status = review_status(conn, account["id"])
    return templates.TemplateResponse(request, "_review_poll.html", {
        "new_cards": [c for c in cards if c["id"] not in shown], "total": len(cards), "oob": True,
        "s": get_settings(), **status})


@app.post("/outreach/{outreach_id}/{action}", response_class=HTMLResponse)
def outreach_action(request: Request, outreach_id: int, action: str, text: str = Form("")):
    account = current(request)
    if not account:
        return HTMLResponse("", status_code=401)
    aid = account["id"]
    htmx = bool(request.headers.get("hx-request"))
    message = ""
    if action in ("save", "approve"):
        if text.strip():
            outreach.save_text(aid, outreach_id, text)
        if action == "approve":
            message = outreach.approve(aid, outreach_id) or ""
            if not message and htmx:
                return HTMLResponse("")  # card leaves the review queue
        else:
            message = "Saved."
    elif action == "skip":
        if outreach.human_move(aid, outreach_id, "drafted", "skipped", reason="Skipped by operator.") and htmx:
            return HTMLResponse("")
    elif action == "unapprove":
        if not outreach.human_move(aid, outreach_id, "approved", "drafted", approved_at=None):
            flash(request, "Too late to pull this back: it is already being sent or was sent.", "error")
    elif action == "delivered":
        outreach.human_move(aid, outreach_id, "needs_check", "sent", sent_at=iso(),
                            reason="Operator confirmed on X that it was delivered.")
    elif action == "not-delivered":
        outreach.human_move(aid, outreach_id, "needs_check", "approved", approved_at=iso(), sending_at=None,
                            reason="Operator confirmed on X that it was NOT delivered; queued again.")
    if htmx and action in ("save", "approve"):
        return review_card(request, account, outreach_id, message)
    if message:
        flash(request, message)
    return back(request)


@app.post("/replies/{event_id}/handled")
def reply_handled(request: Request, event_id: int):
    account = current(request)
    if account:
        with db() as conn:
            conn.execute("UPDATE dm_events SET handled=1 WHERE id=? AND account_id=?", (event_id, account["id"]))
    return back(request)


# ------------------------------------------------------------------ account

@app.post("/account/{action}")
def account_action(request: Request, action: str, confirm: str = Form("")):
    account = current(request)
    if not account:
        return login_redirect()
    if action == "pause":
        accounts.set_paused(account["id"], True)  # the page shows a standing "stopped" notice; no flash needed
    elif action == "resume":
        accounts.set_paused(account["id"], False)
        flash(request, "Sending resumed.")
    elif action == "delete":
        if confirm.strip().lower() != account["handle"].lower():
            flash(request, "Type your handle exactly to confirm deletion.", "error")
            return back(request)
        accounts.disconnect_and_delete(account["id"])
        request.session.clear()
        flash(request, "Account disconnected and its data deleted.")
        return login_redirect()
    return back(request)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    account = current(request)
    if not account:
        return login_redirect()
    with db() as conn:
        usage = quota.summary(conn, account["id"])
    return render(request, "settings.html", account, usage=usage)
