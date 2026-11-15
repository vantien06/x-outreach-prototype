# Warm Outreach

Warm Outreach helps you find AI and robotics people on X and start a real conversation with them. You describe who you are looking for. It finds people, shows why each one fits, and writes a personal DM for each, based on what they actually posted. You read every message first, and only the ones you approve are sent. Nobody is ever messaged twice.

## Two ways to try it

**1. Use the hosted version.** Open **<https://outreach.vantien.tech>**, type the invite code from my email, and sign in with your X account. There is nothing to install.

**2. Run it on your own computer.** Follow the six steps below, in order. It takes about 10 to 15 minutes, and most of that is creating the X app.

## Run it on your own computer

### Step 1. Get the code

Install [Git](https://git-scm.com) and [uv](https://docs.astral.sh/uv/) if you do not have them. To install uv:

- macOS or Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

Then download the project and install what it needs:

```bash
git clone https://github.com/vantien06/x-outreach-prototype.git
cd x-outreach-prototype
uv sync
```

### Step 2. Create an X developer app

The tool talks to X through your own developer app.

1. Go to the [X Developer Console](https://console.x.com) and sign in with your X account.
2. Open **Apps**, then open your app (create one if the list is empty).
3. Click **Settings** and fill in the form like this:

   | Field | What to enter |
   |---|---|
   | App permissions | Read and write and Direct message |
   | Type of App | Web App, Automated App or Bot |
   | Callback URI | `http://127.0.0.1:8000/auth/callback` |
   | Website URL | Any public page of yours, such as your GitHub profile |

4. Click **Save Changes**.
5. Copy the **Client ID** and **Client Secret** and keep them somewhere for Step 4.
6. Open **Credits** and add $5.

### Step 3. Get an Anthropic API key

The tool uses Claude to judge who fits and to write the drafts.

1. Go to <https://console.anthropic.com> and sign in.
2. Create an API key and copy it.
3. Make sure the account has a few dollars of credit.

### Step 4. Add your keys

1. In the project folder, make a copy of the file `.env.example`.
2. Rename the copy to `.env`.
3. Open `.env` in any text editor and fill in these three lines:

```
X_CLIENT_ID=your client id from Step 2
X_CLIENT_SECRET=your client secret from Step 2
ANTHROPIC_API_KEY=your key from Step 3
```

Save the file.

### Step 5. Start the tool

```bash
uv run uvicorn app.main:app --port 8000
```

Leave this window open while you use the tool. To stop it, press `Ctrl+C`.

### Step 6. Sign in

1. Open **<http://127.0.0.1:8000>** in your browser.
2. Click **Sign in with X** and approve the app with the X account you want to send from.
3. Make sure **Chat** is turned on for both the sending account and the receiving account. X asks each account to set a PIN the first time it opens Chat.

## Using it

1. On the dashboard, start a **New campaign**. Describe the people you want to reach, or choose **Seed accounts** and enter a few handles to look through the people they follow. Then write one or two sentences about who you are; every draft introduces you with those words.
2. Click **Find people**. Each person comes with a fit score, a short reason, and the bio or posts behind it.
3. Tick the people you like and click **Draft DMs for selected**.
4. Open the **Review queue**. Read each draft next to the evidence it is based on. Edit it if you want, then **Approve and queue** it, or **Skip** it. A skipped draft is kept and can be brought back.
5. Approved messages are sent one at a time. The **Dashboard** shows what was sent, who replied, and anything that needs you. **Stop sending** pauses everything immediately.

Want to try a real send without messaging a stranger? Use a second X account of your own. Follow it from your sending account, then run a **Seed accounts** campaign with your sending account's handle as the seed.

## If something goes wrong

| What you see | How to fix it |
|---|---|
| The X Console says "Not a valid URL format" | The Website URL has to be a public `https://` address, not `localhost`. Leave the optional fields empty. |
| X shows a callback error after you approve | Check that the Callback URI is exactly `http://127.0.0.1:8000/auth/callback` and that you opened the tool at `http://127.0.0.1:8000`. |
| "Sign-in with X is not set up" | The keys in `.env` are missing, or the tool was started before you saved the file. Stop it and start it again. |
| The tool does not start because port 8000 is in use | Close the other program that is using port 8000, then start again. |
| Most people show "DMs closed to you" | This comes from X. Many people only accept DMs from accounts they follow. |
| The tool asks you to sign in again | The same X account signed in to another copy of the tool, such as the hosted version. Use each account with one copy at a time. |

## Good to know

- If someone does not follow you, your first message waits in their **Message requests** until they accept it.
- X encrypts replies. The tool tells you who replied and when, and gives you a link to read the reply on X.
- Search only looks at the last 7 days of posts. For a narrow group, seed accounts usually find better matches.
- Sending is slow on purpose: at most 20 messages a day, 90 seconds apart. You can change this in `.env`.
- X charges for what the tool reads and sends. A quick search costs about $0.30 and each DM $0.015.

More detail is in [docs/REFERENCE.md](docs/REFERENCE.md): every setting, hosting it yourself, what X allows, costs and limits.
*A take-home assignment by Van Tien Nguyen for the GenAI Fund Forward Deployed Engineer role.*
