# Warm Outreach

A small tool for running personal DM outreach on X to AI and robotics builders, founders and researchers. You give it a target segment or a few seed accounts. It finds people, shows why each one fits with the bio or post that reason rests on, and drafts a first message that only says things the evidence supports. Nothing is sent until you approve it. Approved messages go out one at a time from your own connected account, never twice to the same person, and the dashboard shows what was sent, who replied and what needs you.

It is a prototype: one process, SQLite, server-rendered pages. The limits are listed [below](#limitations-and-what-is-not-built).

## What you need

| | Needed for | Notes |
|---|---|---|
| Python 3.11+ and [uv](https://docs.astral.sh/uv/), or Docker | everything | |
| Nothing else | dry run | Sample data, no keys, no network calls |
| An X developer app | live use | Pay-per-use, with credit loaded. Details below |
| An Anthropic API key | live use | Claude Sonnet 5.5 writes drafts; Claude Haiku 5.5 writes search queries, scores fit and labels replies |
| An X account to send from | live use | It must have X Chat set up (the passcode X asks for when you first open Chat) |

### Setting up the X developer app

In the [X Developer Console](https://developer.x.com), create an app and open its **User authentication settings**:

1. **App permissions:** Read and write and Direct message.
2. **Type of App:** Web App, Automated App or Bot (confidential client).
3. **Callback URI:** `<BASE_URL>/auth/callback`. For local use that is `http://127.0.0.1:8000/auth/callback`. X does not accept `localhost` reliably, so use `127.0.0.1` and open the app at that address too.
4. **Website URL:** any public URL you own. It is only shown on the consent screen.
5. Save, then copy the **OAuth 2.0 Client ID** and **Client Secret**.
6. Load credit under Billing. A few dollars is enough to try everything.

The tool asks for these scopes at sign-in: `tweet.read users.read follows.read dm.read dm.write offline.access`.

## Run it

### 1. Dry run (no keys, about a minute)

```bash
uv sync
uv run uvicorn app.main:app --port 8000
```

Open <http://127.0.0.1:8000>. Dry run is the default. Sign-in is simulated, the people are fictional, sends and replies are simulated, and nothing is billed. It walks the whole flow, including a refused DM, an uncertain send, an encrypted reply and an opt-out.

### 2. Local, with your own keys

```bash
cp .env.example .env
```

Edit `.env`:

```
DRY_RUN=false
BASE_URL=http://127.0.0.1:8000
X_CLIENT_ID=<your client id>
X_CLIENT_SECRET=<your client secret>
ANTHROPIC_API_KEY=<your key>
```

Then either:

```bash
uv sync && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

or, with Docker:

```bash
docker compose up --build
```

Open <http://127.0.0.1:8000> and choose **Sign in with X**. In local mode there is one operator and every key is yours. The database and the generated token-encryption key live in `./data`.

### 3. Hosted (Railway)

In hosted mode the host's X app and Anthropic key are used, and each user only signs in with their own X account. Because the host pays for every call, sign-in is limited to an allowlist and there is a spend ceiling.

1. Create a Railway project from this repository. The `Dockerfile` is picked up automatically.
2. Attach a **volume** mounted at `/data`.
3. Generate a public domain for the service.
4. Add the callback `https://<your-domain>/auth/callback` to the X app.
5. Set these variables, then deploy:

```
APP_MODE=hosted
DRY_RUN=false
BASE_URL=https://<your-domain>
DATABASE_PATH=/data/outreach.db
X_CLIENT_ID=...
X_CLIENT_SECRET=...
ANTHROPIC_API_KEY=...
TOKEN_ENCRYPTION_KEY=<output of the command below>
ALLOWED_HANDLES=<handle1>,<handle2>
GLOBAL_BUDGET_USD=10
```

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run exactly one instance. The sender runs inside the web process and SQLite has a single writer.

## Configuration

Everything is set through environment variables or `.env`. `.env.example` lists them all with comments.

| Variable | Default | Meaning |
|---|---|---|
| `APP_MODE` | `local` | `local`: one operator, own keys. `hosted`: many users, host's keys, allowlist required |
| `DRY_RUN` | `true` | `true` makes no X or LLM calls at all |
| `BASE_URL` | `http://127.0.0.1:8000` | Public address of the app; the OAuth callback is built from it |
| `X_CLIENT_ID`, `X_CLIENT_SECRET` | | From your X developer app |
| `ANTHROPIC_API_KEY` | | Claude API key |
| `TOKEN_ENCRYPTION_KEY` | generated in local mode | Fernet key that encrypts stored X tokens. Required in hosted mode |
| `DATABASE_PATH` | `data/outreach.db` | SQLite file. Point it at the volume when hosted |
| `ALLOWED_HANDLES` | empty | Hosted only: X handles allowed to sign in. Empty admits nobody |
| `DAILY_SEND_CAP` | `20` | DMs per connected account per UTC day |
| `SEND_SPACING_SECONDS` | `90` | Pause between two sends from the same account |
| `DAILY_CANDIDATE_CAP` | `80` | People scored per account per UTC day (`0` = no cap) |
| `GLOBAL_BUDGET_USD` | `0` | Estimated spend after which discovery, drafting and sending lock for everyone (`0` = no ceiling) |
| `MAX_QUERIES`, `POSTS_PER_QUERY`, `MAX_POSTS_PER_CAMPAIGN` | `3`, `20`, `60` | Bounds on what one Quick segment search may read |
| `MAX_CANDIDATES_PER_CAMPAIGN` | `15` | People scored per Quick campaign |
| `WIDE_SEARCH_FACTOR` | `3` | A Wide search multiplies the four limits above by this |
| `SEED_FOLLOWING_LIMIT`, `SEED_POSTS_PER_CANDIDATE` | `100`, `5` | Bounds on a seed campaign |
| `CHAT_READS_PER_15MIN`, `CHAT_READS_PER_DAY`, `CHAT_READS_PER_TICK` | `60`, `6000`, `8` | Self-imposed limits on reply checks |

## Using it

1. **Sign in with X** using the account that will send.
2. **New campaign.** Pick one:
   - *Target segment*: describe who you want ("founders and engineers building AI agents in Southeast Asia"). The tool writes searches over the last 7 days of posts that pair first-person phrasing ("I'm building", "we shipped") with the segment's own terms, so they favour people describing their own work over accounts that only talk about the topic.
   - *Seed accounts*: enter a few handles. The tool looks at who they follow.

   Also say who you are and why you are writing. Drafts only say about you what you write here.

   *Search size* (segment campaigns): **Quick** is the default: 3 searches, at most 60 posts, at most 15 people scored, about $0.31. **Wide** is about three times the people for about three times the cost. The form shows both estimates.
3. **Look at the people found.** Each has a fit score and a short reason. Open *Evidence* to see the bio and posts the reason rests on; each item is a link to that post or profile on X. Nobody is preselected: tick the people you want a draft for. The line at the top gives the estimated cost; *Search details* lists the searches that ran and who was left out and why.

   How people are chosen for scoring: accounts whose name or handle marks them as an organisation, news feed, bot or promotion are dropped first. Everyone else gets a free pre-score from their bio and the posts already read, using two word lists the model writes for the segment (signs that someone does this work, and signs that an account only talks about it) on top of built-in ones. A bio counts more than posts. People are ranked by that pre-score, with "X says you can message them" as a tie-break, and only the top of the list is scored by the model. Accounts that read as clearly off-target are left out and named in *Search details*.
4. **Draft DMs for selected.** Drafts appear in the review queue as they are written.
5. **Review queue.** Each draft sits next to its evidence, with a list of the statements it makes about the person and which item backs each one. Edit, approve or skip. A statement the model could not tie to stored evidence is flagged.
6. **Dashboard.** Approved messages are sent one at a time with a pause between them. You see:
   - *Needs your attention*: replies, and any send whose outcome is uncertain.
   - *Blocked*: not sent, with the reason.
   - *Sent*: one row per person. Open it to see the conversation: your message, whether they opened the thread, and their reply.
   - **Stop sending** in the sidebar halts the queue at once.
7. **Settings** shows usage and cost estimates, how sending works on X, and a button to disconnect and delete your data.

## What X does and does not allow

These were measured against the live API on 14 November 2026 with accounts under our control. They shape what the tool can do.

- **A first DM to someone who does not follow you lands in their Message requests.** They have to accept it before it becomes a normal conversation. The tool cannot change that.
- **Many accounts are closed to DMs from strangers.** X reports this per person, and the tool shows "DMs closed to you" next to their name. In our test with a new, unverified sending account, most people found by search were closed. An established or verified account should reach more people; we could not test that.
- **Send limits.** X's response headers allowed 1,440 DMs per 24 hours per user. The tool deliberately stays far below that (20 per day by default, 90 seconds apart). X's rules do not allow bulk unsolicited automated DMs; this tool is built for a person approving each message.
- **Replies typed in the X app are end-to-end encrypted.** The tool can see that a person replied and when, and it can tell a reply apart from merely accepting the request, but it cannot read the text. The dashboard shows "Encrypted reply" with a **Read on X** button. Automatic labelling of replies (interested, question, not interested, opt-out) only works when the text is readable.
- **The tool does not send through encrypted X Chat.** That would require holding the sender's private keys and Chat passcode on the server, which X's documentation tells third-party servers not to do. Messages are sent with the standard DM endpoint and arrive normally.
- **Search covers the last 7 days only**, and every post returned is billed. People who have not posted about their work this week will not be found by a segment search; use seed accounts for them.
- **The sending account needs X Chat set up.** In our test, messages sent before the sender had created a Chat passcode did not appear for the recipient until the passcode existed. We saw this once and did not isolate the cause.
- **Protected accounts** cannot have their posts read unless the sender follows them, so they are scored on the bio alone, or not scored if there is no bio.

## How double-messaging is prevented

The rule is one message per recipient per sending account, ever.

1. **One row per person.** The database has a unique constraint on (sending account, recipient). A second draft for the same person returns the existing row.
2. **People are not listed twice.** Someone already found by any earlier campaign is skipped by later ones.
3. **Claim before send.** A message is moved from *approved* to *sending* inside a write transaction before X is called, so two workers can never both take it.
4. **Existing threads block.** Before sending, the tool checks for an existing DM thread with that person and refuses if there is one.
5. **Uncertain outcomes are never retried.** A timeout or server error leaves the message in *needs check*. Only you, or the message later appearing in the conversation history, can settle it. A definite refusal from X (for example DMs closed) is recorded as *blocked* with the reason and not retried.
6. **Deleting your data keeps a fingerprint.** After "disconnect and delete", a one-way hash of each (sender, recipient) pair remains, so reconnecting cannot message the same people again.

`tests/test_no_duplicates.py` covers these, including eight workers racing for the same message.

## Costs

X's pay-per-use prices below come from third-party summaries, not from an official price list we could read, so check them against your Developer Console. The app's cost figures are estimates built from these numbers.

| Item | Assumed price |
|---|---|
| Post read (search, a person's posts) | $0.005 per post |
| Profile read (seed lookups, following lists) | $0.010 per profile |
| DM sent | $0.015 |
| Reply check (chat history read) | $0.001 (placeholder; real price unknown) |
| Claude Haiku call (queries, scoring, labels) | about $0.0005 |
| Claude Sonnet call (one draft) | about $0.01 |

Measured in the hosted test: three small campaigns plus a repeat run that found nobody new (16 people scored in total), one draft, one DM and its reply checks came to about **$0.37** by the app's own estimate.

Upper bounds per campaign with default settings: a Quick segment search reads at most 60 posts (about $0.30) and scores at most 15 people, about $0.31 in total, which is what a live Quick run measured; a Wide search is about three times that; a seed campaign reads at most 100 profiles (about $1.00) plus 5 posts for each of up to 15 people (about $0.38). Typical runs are well below these. Hosting on Railway is about $5 a month.

## Limitations and what is not built

- **Reply text is not readable when encrypted** (see above), so reply labelling is partial.
- **Telling a reply from "accepted the request"** relies on reading the unencrypted envelope of X Chat events. It was right in both real cases we observed; that is a small sample.
- **No follow-up messages.** One message per person, by design.
- **One sending account per sign-in**, and one app instance. Scaling out needs a real queue and Postgres.
- **Discovery quality depends on a 7-day search window** and on what people write in public. The new, broader query set and paging were tested in dry run and unit tests only, not yet against live X.
- **Not exercised against the live API:** token refresh and revoke, search paging, the profile `location` field, and X's real 403 and 429 responses. The code follows the documented behaviour.
- **Cost figures are estimates**, as explained above.
- **CSRF protection** relies on `SameSite=Lax` session cookies only.
- **Hosted mode stores each user's X tokens** (encrypted) on the host. Users can revoke access from X or with "disconnect and delete".

## Tests

```bash
uv run pytest -q
```

The suite runs without keys or network access. It covers duplicate prevention, the state machine, limits and budget, discovery (queries, post budget, pre-filters, ranking, seed evidence), reply detection, and the review and dashboard pages.

`scripts/live_probe.py` is a manual smoke test against the real X API for the account connected in the app (`PYTHONPATH=. uv run python scripts/live_probe.py <handle>`). It only reads unless you pass `--send`.

## Layout

```
app/main.py            routes and pages
app/services/          discovery, drafting and sending, replies, quotas
app/x/                 X client: real (httpx), mock (dry run), chat event reader
app/llm/               Claude client and the dry-run stand-in
app/worker.py          background sender and reply checker
app/templates, static  server-rendered UI (HTMX)
tests/                 pytest suite
```
