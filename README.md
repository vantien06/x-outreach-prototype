# Warm Outreach

Find AI and robotics people on X, see why each one fits, review a personalised DM for each, and send only the ones you approve. Nobody is messaged twice, and the dashboard shows what was sent, who replied and what needs you.

Give it a target segment or a few seed accounts. Every reason and every draft is tied to the person's own bio or posts, shown next to it. Nothing is sent until you approve it.

**Two ways to try it**

- **Hosted demo:** open the link and enter the invite code from the submission email. Nothing to install.
- **Run it yourself:** follow this page. About 1 minute to see it with sample data, 10 to 15 minutes to send real DMs from your own account.

## 1. See it in one minute (no keys)

You need [Git](https://git-scm.com) and [uv](https://docs.astral.sh/uv/).

```bash
# install uv once, if you do not have it
#   macOS / Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh
#   Windows:        powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone https://github.com/vantien06/x-outreach-prototype.git
cd x-outreach-prototype
uv sync
uv run uvicorn app.main:app --port 8000
```

Open **<http://127.0.0.1:8000>** and choose **Enter dry run**.

This is dry run: fictional people, simulated sends and replies, no keys, nothing billed. It walks the whole flow, including a refused DM, an encrypted reply and an opt-out. Stop it with `Ctrl+C`.

## 2. Go live with your own account (10 to 15 minutes)

You need three things. Steps A and B are the slow part; do them in two browser tabs.

### A. An X developer app (about 5 minutes)

1. Open the [X Developer Console](https://console.x.com) with the X account that will own the app, and create an app (or use the default one).
2. Open the app, then **Settings** (authentication settings), and set:

   | Setting | Value |
   |---|---|
   | App permissions | **Read and write and Direct message** |
   | Type of App | **Web App, Automated App or Bot** |
   | Callback URI | `http://127.0.0.1:8000/auth/callback` |
   | Website URL | any public page of yours, for example your GitHub profile (`localhost` is rejected) |

3. **Save Changes**. Copy the **OAuth 2.0 Client ID** and **Client Secret** when they are shown.
4. Under **Credits**, buy a small amount. US$5 is plenty to try everything.

### B. An Anthropic API key (about 2 minutes)

Create a key at <https://console.anthropic.com> and make sure the account has a little credit. The tool uses Claude Haiku 5.5 to judge fit and Claude Sonnet 5.5 to write drafts.

### C. Put the keys in `.env` (1 minute)

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` and change only these four lines:

```
DRY_RUN=false
X_CLIENT_ID=<your client id>
X_CLIENT_SECRET=<your client secret>
ANTHROPIC_API_KEY=<your key>
```

Leave everything else as it is. `.env` is ignored by Git.

### D. Start it and sign in (1 minute)

```bash
uv run uvicorn app.main:app --port 8000
```

Open **<http://127.0.0.1:8000>** (use `127.0.0.1`, not `localhost`), choose **Sign in with X**, and authorize the app with the account you want to send from.

Prefer Docker? `docker compose up --build` does the same with the same `.env`.

### E. One thing on the sending account

Open **Messages** on x.com with the sending account and create the chat passcode X asks for. Until that is done, messages sent through the API may not reach the other person.

## 3. First run

1. **New campaign** on the dashboard: describe who you want to reach, or switch to *Seed accounts* and enter a few handles whose follows you want to look through. Say who you are in your own words; drafts introduce you with exactly that.
2. **Find people.** Each person appears with a fit score, a short reason, and the bio or posts behind it. "DMs closed to you" means X will not let this account message them.
3. Tick the people you want and choose **Draft DMs for selected**.
4. **Review queue:** each draft sits next to the evidence it uses. Edit, **Approve and queue**, or **Skip** (skipped drafts are kept and can be put back).
5. Approved messages go out one at a time. The **Dashboard** shows what was sent, who replied, what was blocked and why. **Stop sending** halts everything at once.

To test real sending without contacting strangers, use a second X account you control: follow it from the sending account, then run a *Seed accounts* campaign with the sending account's own handle as the seed.

## How long setup takes

Timed on a fresh clone of this repository (Windows 11, home broadband):

| Step | Time |
|---|---|
| `git clone` | 3 s |
| `uv sync` with an empty cache | 17 s |
| Start to first page | 1 s |
| **Dry run usable** | **under 1 minute** |
| Whole test suite (`uv run pytest`) | 22 s |
| `docker compose up --build`, first time | about 2 minutes |
| Create the X app and copy its keys | about 5 minutes (by hand, not timed by machine) |
| Create the Anthropic key | about 2 minutes (by hand) |
| Edit `.env`, start, sign in | about 2 minutes (by hand) |
| **Live and signed in** | **about 10 to 12 minutes** |

## If something goes wrong

| What you see | What to do |
|---|---|
| X Console says "Not a valid URL format" | The Website URL must be a public `https://` address, not `localhost`. Leave the optional fields empty. |
| After authorizing, X shows an error about the callback | The Callback URI in the X app must be exactly `http://127.0.0.1:8000/auth/callback`, and you must open the app at `http://127.0.0.1:8000`. |
| "Sign-in with X is not set up" | `X_CLIENT_ID` is empty in `.env`, or the app was started before you edited it. Restart it. |
| The page still says **DRY RUN** | `DRY_RUN=false` is not set in `.env`, or the app was not restarted. |
| Port 8000 is busy | Another program uses it. Stop that program; the callback is registered for port 8000. |
| Everyone shows "DMs closed to you" | That is X, not the tool: many people only accept DMs from accounts they follow. A verified or better-known sender reaches more people. |
| The dashboard asks you to sign in again | The same X account signed in to another copy of this app (for example the hosted demo). One X account can be connected to one copy at a time. |

## What to know before you rely on it

- A first message to someone who does not follow you lands in their **Message requests**.
- Replies typed in the X app are end-to-end encrypted. The tool shows who replied and when, and links to the conversation; the text is read on X.
- Search covers the last 7 days of posts. Seed accounts usually find better-matched people than a keyword search for a narrow region.
- Sending is deliberately slow and capped (20 a day, 90 seconds apart by default). Change the limits in `.env`.
- X bills every post and profile the tool reads and every DM it sends. A Quick search costs about US$0.30.

Everything else is in **[docs/REFERENCE.md](docs/REFERENCE.md)**: hosting on Railway and the invite code, every setting, what X allows, how double-messaging is prevented, costs, limitations and tests. The one-page write-up is in **WRITEUP.md**.

*A take-home assignment by Van Tien Nguyen for the GenAI Fund Forward Deployed Engineer role. A working prototype, not a product.*
