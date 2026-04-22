# PulseCall

> Outbound political polling on the SignalWire platform — voter lists, a real-time
> operator dashboard, live SSE drilldown, DNC-first compliance, and proper
> inbound callback handling. Built with the [SignalWire SDK](https://github.com/signalwire/signalwire-python).

PulseCall lets you define a short poll (a handful of questions with canonical
answer choices), upload one or more voter lists (optionally segmented by gender /
age band / party), and run an outbound dialing campaign that records each
respondent's answers against your question bank. An operator dashboard shows
progress in real time without ever polling when the campaign is idle, and a
dedicated DNC registry (local + integrated with inbound STOP replies) is checked
on every dial.

---

## Features

**Campaign design**
- Consent/intro script + ordered question bank per campaign
- 12 canonical question presets covering the forms polls actually take:
  right-direction / wrong-track, top-issue priority (with "Something else"),
  4-pt and 5-pt approval, outlook 5-pt, trust 4-pt, Likert 5-pt,
  yes/no/unsure, yes/no (strict), numeric scale 1–5, open-ended, custom multi
- Freeze-on-run: question set is locked once the campaign leaves `draft` so
  reports stay coherent; name / intro / caller-ID / concurrency stay editable

**Voter lists**
- Lists are first-class objects, shared across campaigns
- Per-voter demographics (gender, age band, party, ZIP)
- CSV paste upload (headers optional), inline edit-in-place, gender filter
- Bulk import pre-filters against the DNC registry at insert time

**Dialer**
- Queue built from `campaign_voter_state` (per-(campaign, voter) status)
- DNC gate at queue build AND one more time immediately before each dial
- Per-campaign concurrency cap; 0.5s spacing between dials
- Graceful failure on missing creds / REST errors — no phantom calls
- APScheduler-driven drains with a "kick" thread that exhausts the queue
  when dials complete synchronously instead of waiting 30s

**Inbound callbacks** (`/inbound` — wire this to your SignalWire number)
- Identifies caller by E.164 number, branches by state:
  - In DNC → confirm removal, offer re-opt-in, goodbye
  - Pending voter → offer to take the poll now
  - Completed voter → thank, offer DNC
  - Unknown → up-front identification, offer DNC
- Caller can always say "don't call me"; SMS `STOP` also opts them out

**Real-time dashboard without idle polling**
- Per-call Q&A feed and per-campaign progress stream over Server-Sent Events
- Streams close themselves (`event: ended`, `event: idle`) when there's
  nothing live — no open connections, no polling, zero cost when quiet

**Reporting & exports**
- Cross-campaign rollup: total calls, completion / DNC / failure rates,
  per-campaign cards with completion bars
- Per-question distributions with percentages and counts
- CSV exports for answers (flat, one row per response with voter/question
  metadata) and voters (roster with dial status)

**Operations**
- Retry-failed: safe in production — flips `failed` + stuck `calling`
  voters back to `pending` and resumes
- Reset: destructive testing action — wipes calls/answers, flips all
  non-DNC voters to `pending`, campaign returns to `draft`
- Crash/restart recovery: running campaigns are rediscovered on startup;
  stuck `calling` rows are reclaimed; any campaign left `running` with no
  active voters is auto-rescued to `completed`

**DNC (do-not-call) management**
- Global registry — DNC applies across every campaign and list
- In-call `mark_dnc` tool (voice-triggered) with confirmation SMS
- Inbound `STOP` SMS webhook (`/sms-webhook`) mirrors 10DLC opt-out into
  the local registry so the dialer sees it
- Belt-and-suspenders: the `record_answer` tool also detects DNC phrases
  in the submitted value and re-routes to opt-out
- Full management UI: filter, add, bulk CSV, delete

---

## Architecture

```
                     ┌──────────────────────────────┐
                     │  Operator dashboard (web/)   │
                     │  campaigns · lists · voters  │
                     │  call feed · reports · DNC   │
                     └──────────────┬───────────────┘
                                    │ REST + SSE
                                    ▼
┌──────────────────────────────────────────────────────────────┐
│ FastAPI app (AgentServer.app) — server.py                    │
│                                                              │
│  /outbound  /inbound      (SignalWire SWML webhooks)         │
│  /sms-webhook             (inbound STOP opt-outs)            │
│  /api/campaigns           /api/lists        /api/voters      │
│  /api/dnc                 /api/reports/overview              │
│  /api/calls/{id}/events   /api/campaigns/{id}/events  (SSE)  │
└──────┬───────────────────┬──────────────────────────┬────────┘
       │                   │                          │
       ▼                   ▼                          ▼
┌────────────┐     ┌───────────────┐        ┌──────────────────┐
│  SQLite    │◀────│ OutboundDialer│───────▶│ SignalWire REST  │
│ pulsecall  │     │ (APScheduler) │        │ /api/calling     │
│    .db     │     │ respects DNC  │        │    /calls        │
└────────────┘     └───────────────┘        └──────────────────┘
       ▲
       │  in-process pub/sub (asyncio.Queue per topic)
       │
┌──────┴─────────────────────────────────────────────────────┐
│ Agents (PollingAgent, InboundAgent) — AgentBase subclasses │
│  contexts/steps: greeting → [consent | offer] →            │
│                   ask_question loop → wrap_up_*            │
│  SWAIG tools: record_answer, skip_question, mark_dnc,      │
│               give_consent, accept_poll_offer,             │
│               remove_from_dnc, wrap_dnc_confirmed          │
│  on_summary → notify_call_ended → dialer kick              │
└────────────────────────────────────────────────────────────┘
```

**Why this shape.** The SignalWire SDK does the heavy lifting for telephony and
AI orchestration — PulseCall adds the domain layer (poll flow, DNC, reporting)
on the same FastAPI app the SDK already exposes. A thin pub/sub layer between
the agents and the SSE endpoints lets the UI push in real time without the
usual "poll every 30 seconds" pattern, and closes streams the moment there's
nothing to report so idle campaigns cost nothing.

---

## Data model

```
voter_lists
  ├── voters (list_id, phone unique-in-list, gender/age_band/party)
  │
campaigns (status: draft | running | paused | completed)
  ├── questions (ordered, typed: yesno | multi | scale | open)
  ├── campaign_lists  ──────(m:n)─────▶  voter_lists
  └── campaign_voter_state (per-(campaign, voter) dial status: pending |
                            calling | completed | failed | dnc | optout)

calls (one per outbound/inbound attempt) ─▶ answers (one per response)

dnc_list (global; cascades into every campaign_voter_state touching the phone)
```

A voter's reach status is recorded per campaign (`campaign_voter_state`), not on
the voter row — so the same voter list can feed multiple campaigns with
independent dial state, and voters can be edited without rewriting history.

---

## Quick start

### 1. Prerequisites

- Python 3.10+ (3.14 tested)
- A SignalWire account, a phone number, and an API token
- For local testing: [ngrok](https://ngrok.com/) (or any TLS-terminating tunnel)
  so SignalWire can reach your laptop

### 2. Install

```bash
git clone git@github.com:briankwest/pulsecall.git
cd pulsecall
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# edit .env with your SignalWire credentials
```

Minimum `.env` you need:

```ini
SIGNALWIRE_PROJECT_ID=...
SIGNALWIRE_API_TOKEN=...
SIGNALWIRE_SPACE=yourspace.signalwire.com
SIGNALWIRE_PHONE_NUMBER=+15551234567

# HTTP Basic auth guarding /outbound and /inbound. Set any value; SignalWire
# will use these when fetching SWML.
SWML_BASIC_AUTH_USER=pulsecall
SWML_BASIC_AUTH_PASSWORD=<strong-password>

# Public URL SignalWire can reach (e.g. ngrok tunnel)
SWML_PROXY_URL_BASE=https://yoursubdomain.ngrok.io
```

### 4. Run

```bash
python server.py
# → Serving on http://0.0.0.0:3000
```

Open `http://localhost:3000/` in a browser.

### 5. Wire the phone number (one-time, in the SignalWire dashboard)

For the number you want to use:

| Section | Field | Value |
|---|---|---|
| Voice | When a call comes in → URL | `{SWML_PROXY_URL_BASE}/inbound` |
| Voice | HTTP Username / Password | `SWML_BASIC_AUTH_USER` / `_PASSWORD` |
| Messaging | A message comes in → URL | `{SWML_PROXY_URL_BASE}/sms-webhook` |

The `/outbound` endpoint is **not** the number's Voice URL — it's the URL our
dialer hands SignalWire as the `url` parameter of `calling.dial()` when we
place an outbound call. Pointing your phone number's Voice URL at `/outbound`
is a common misconfiguration; you want `/inbound` there.

### 6. Run a poll

1. Go to **Voter lists** → New list → upload a CSV (header optional):

   ```
   phone,first_name,last_name,gender,age_band,party
   +15551110001,Jane,Doe,F,35-44,IND
   +15551110002,John,Smith,M,45-54,DEM
   ```

2. Back on the dashboard → **New campaign** → fill in name + consent
   script, pick the list(s), add questions with presets, **Create**.
3. On the campaign page → **Start**. The dialer begins placing calls,
   respecting concurrency and the DNC registry.
4. Click any voter's **view** link to watch their call live.

---

## UI tour

| Page | What |
|---|---|
| `/` | Campaign list with progress (done / pending / calling / DNC) |
| `/campaign.html?id=N` | Per-campaign drilldown: voter table, per-question results with percentages, CSV exports, Start / Pause / Retry-failed / Reset, live SSE |
| `/call.html?id=CALL_ID` | Live per-call Q&A feed; stream closes on `ended` |
| `/lists.html` | All voter lists with demographics breakdown |
| `/list.html?id=N` | Roster + CSV paste upload + inline edit + gender filter |
| `/reports.html` | Cross-campaign rollup: total calls, completion %, DNC %, per-campaign cards, export links |
| `/dnc.html` | DNC registry: filter, manual add, bulk CSV, delete |

See [`URLS.md`](./URLS.md) for the full REST + SSE reference.

---

## Operations

### Retry a failed run
Some calls will fail — no-answer, dial errors, voicemail hit without consent.
Click **Retry failed** on the campaign page: all `failed` rows (and any rows
stuck mid-`calling` from a crash) are flipped back to `pending` and the drain
restarts. DNC / completed rows are untouched.

### Reset a campaign for testing
On a test campaign, **Reset** wipes every call record + answer and flips all
non-DNC voters back to `pending`. The campaign returns to `draft`. DNC entries
are preserved. The UI confirms before firing.

### Crash / restart recovery
Running campaigns are rediscovered on startup:
- Any row stuck in `calling` → reclaimed to `pending`
- Any campaign left `running` with no active voters → auto-completed
- Scheduler jobs are re-registered and a drain is kicked immediately

### Scheduled drains (optional)
`OUTBOUND_SCHEDULE` in `.env` accepts a 5-field cron spec. If set, every
`running` campaign is kicked on that schedule (in addition to the normal
per-start drain). Leave empty for manual-start only.

### DNC
- **Global, not per-campaign.** A voter who opts out is out of every campaign.
- Writes happen from four places: inbound SMS `STOP`, the in-call `mark_dnc`
  tool, the `record_answer` DNC-phrase detector, and manual entries in the UI.
- On DNC add, every `campaign_voter_state` row touching that phone cascades to
  `dnc` so in-flight campaigns skip the number on the next drain.

---

## Deployment

A `Procfile` is included for Heroku / Dokku / similar:

```
web: gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --worker-class uvicorn.workers.UvicornWorker
```

Use **one worker**. PulseCall keeps scheduler state, the event-loop-capture
for SSE, and the in-process pub/sub in memory — multi-worker requires shared
state (Redis, etc.) which isn't wired in. For scale, run one web dyno with
adequate cores and rely on async concurrency; the bottleneck is outbound
REST round-trips, not Python work.

### Environment

See `.env.example` for the complete list. Beyond SignalWire and SWML:

| Var | Default | What it does |
|---|---|---|
| `AI_MODEL` | `gpt-oss-120b` | Model the agent asks SignalWire's AI to use |
| `AI_TOP_P` / `AI_TEMPERATURE` | `0.5` / `0.3` | Sampling |
| `MAX_OUTBOUND_CONCURRENT` | `2` | Default concurrency; overridable per campaign |
| `OUTBOUND_SCHEDULE` | *(empty)* | Optional 5-field cron for periodic drains |
| `DATABASE_PATH` | `pulsecall.db` | SQLite file, relative to the app dir |
| `HOST` / `PORT` | `0.0.0.0` / `3000` | Server bind |

---

## Project layout

```
pulsecall-app/
├── agent.py              PollingAgent (outbound) — greeting + give_consent
├── inbound_agent.py      InboundAgent — DNC/pending/completed/unknown branching
├── agent_shared.py       ask_question loop + record_answer/skip/mark_dnc
├── dialer.py             OutboundDialer — APScheduler, kick-drain, recovery
├── db.py                 SQLite WAL schema + CRUD + exports
├── events.py             in-process pub/sub for SSE
├── server.py             FastAPI app, lifespan, REST + SSE routes
├── config.py             .env loader
├── requirements.txt
├── Procfile
├── .env.example
├── URLS.md               Full URL reference
└── web/
    ├── index.html        Campaign list + create dialog with presets
    ├── campaign.html     Drilldown + edit + retry/reset + live SSE
    ├── call.html         Live per-call Q&A feed
    ├── lists.html        All voter lists
    ├── list.html         Voter roster + upload + edit
    ├── reports.html      Cross-campaign rollup
    ├── dnc.html          DNC registry management
    └── sounds/typing.mp3 SWAIG wait-file played while tools run
```

---

## Development

### Running locally against a real phone

```bash
# In one terminal:
ngrok http 3000
# Copy the https URL into .env as SWML_PROXY_URL_BASE, then:
python server.py
```

In the SignalWire dashboard, point your number's Voice URL at
`{ngrok}/inbound` with Basic Auth set to your `SWML_BASIC_AUTH_*` values.

### Running without real creds
PulseCall will happily run end-to-end without valid SignalWire credentials —
dials fail gracefully (voter marked `failed`, no phantom calls) and you can
still exercise every screen, the reset/retry flows, and the DNC UI.

### Adding a new question preset
Edit the `QUESTION_PRESETS` object in `web/index.html`. Each preset defines
a `type` (one of `multi`, `yesno`, `scale`, `open`), optional `choices`, and
a `confirm` default. The backend stores what the preset emits — `answer_type`
+ `choices_json`.

### Adding a new agent tool
Tools live in `agent_shared.register_shared_tools` (shared across both agents)
or in each agent's `_define_tools` method. Use the `@self.tool(...)` decorator
and return `SwaigFunctionResult`. Tools have access to `raw_data["global_data"]`
(per-call context populated by `_per_call_config`).

---

## Compliance

PulseCall enforces several layers of opt-out:

1. **DNC pre-check** before every dial (queue build AND immediately before the
   REST `calling.dial()`)
2. **In-call voice opt-out** via the `mark_dnc` tool, which confirms politely
   and sends a confirmation SMS
3. **Inbound `STOP` SMS** wired to `/sms-webhook` → writes DNC registry
4. **DNC is global** — opting out of one campaign opts out of all
5. **Belt-and-suspenders**: `record_answer` detects DNC phrases in submitted
   answers and re-routes, in case the model misroutes

You are responsible for your jurisdiction's rules (TCPA in the US, CASL in
Canada, PECR in the UK, etc.) — PulseCall gives you the primitives to comply
but does not on its own make a campaign legal. Be especially careful about
time-of-day restrictions, robocall regulations, state-level registries, and
quiet-hours rules.

---

## Tech stack

- **[SignalWire SDK](https://github.com/signalwire/signalwire-python)** — telephony, SWAIG, AI agent primitives
- **FastAPI + uvicorn** — HTTP + SSE
- **APScheduler** — outbound dialer scheduling
- **SQLite (WAL mode)** — persistence; single-process, zero-ops
- **Vanilla HTML + JS** — no build step, no framework

---

## License

MIT
