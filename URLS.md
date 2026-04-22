# PulseCall — URL reference

All public-facing URLs exposed by the server and how to wire them into SignalWire.

Throughout this document `{base}` is the URL PulseCall is reachable at — typically
`https://pulsecall.yourdomain.com` in production or `https://abc123.ngrok.io` during
development. Port `3000` is the default (`PORT` in `.env`).

## Quick wiring summary

| SignalWire config field | Value |
|---|---|
| Phone number **Voice → When a call comes in → URL** | `{base}/inbound` |
| Phone number **Voice → Recording status callback** | *(not needed — SDK handles recording)* |
| Phone number **Messaging → A message comes in → URL** | `{base}/sms-webhook` |
| Outbound call `url` parameter *(set by our dialer, not by you)* | `{base}/outbound?campaign_id=…&voter_id=…` |

Both `/inbound` and `/outbound` require HTTP Basic Auth. Set `SWML_BASIC_AUTH_USER`
and `SWML_BASIC_AUTH_PASSWORD` in `.env`. For `/outbound` our dialer embeds those
credentials in the callback URL so SignalWire's fetcher authenticates correctly.
For `/inbound`, enter the credentials in the SignalWire number settings.

---

## Voice / SWML endpoints

### `POST /outbound` — outbound polling agent

**Called by:** SignalWire, when a call placed by our dialer connects.

**Called by us:** never directly — `dialer.py` posts to SignalWire's
`/api/calling/calls` with `command=dial` and `url={base}/outbound?campaign_id=X&voter_id=Y`.

**Query params (set by dialer):**
- `campaign_id` — which campaign this call belongs to
- `voter_id` — which row in the `voters` table

**What it does:** identifies the campaign and voter, last-chance DNC check,
reads the campaign script, asks for consent, then walks the questions list.
On completion, writes a row to `calls` and sets the voter's status to
`completed` / `dnc` / `failed`.

**Auth:** HTTP Basic (`SWML_BASIC_AUTH_USER` / `SWML_BASIC_AUTH_PASSWORD`).
The dialer embeds these in the URL so no extra SignalWire config is needed.

### `POST /inbound` — inbound callback agent

**Called by:** SignalWire, when someone dials our number.

**What it does:** looks up the caller by `caller_id_number` in the `voters` table:
- If the number is in `dnc_list` → confirms they're on the list and offers re-opt-in.
- If the caller is a *pending* voter → offers to take the pending poll now.
- If the caller already *completed* the poll → thanks them and offers to opt out.
- If the number is unknown → up-front introduction + offer to join DNC.

The caller can always say "don't call me" — the agent invokes `mark_dnc` and writes
the number to `dnc_list`, then sends a confirmation SMS.

**Auth:** HTTP Basic. Enter `SWML_BASIC_AUTH_USER` / `SWML_BASIC_AUTH_PASSWORD` in
the SignalWire phone-number voice-settings **HTTP Username / Password** fields.

### SignalWire-auto-mounted sub-routes on each agent

The SDK adds these helper routes under each agent. You don't configure them in
SignalWire's dashboard — SignalWire hits them during the call lifecycle:

- `POST /outbound/swaig` — SWAIG tool webhook
- `POST /outbound/post_prompt` — post-call summary delivery
- `POST /outbound/check_for_input` — AI check-for-input polling
- `GET  /outbound/debug` — live SWML introspection (returns generated SWML)
- `POST /outbound/debug_events` — per-call debug events
- *(and the same set under `/inbound/...`)*

---

## SMS webhook

### `POST /sms-webhook`

**Called by:** SignalWire when someone texts our phone number.

**Wire it at:** SignalWire phone number **Messaging → A message comes in → URL**.

**Body (form-encoded or JSON):** the SignalWire Compat SMS payload. PulseCall reads
`From` (or `from` / `from_number`) and `Body` (or `body` / `message`).

**What it does:** if the text body is `stop`, `stop all`, `unsubscribe`, `cancel`,
`quit`, or `end`, we add the `From` number to `dnc_list` with `reason='inbound_stop'`.
All other messages are ignored (for now).

Returns `200 {"ok": true}` regardless so SignalWire doesn't retry.

---

## REST API (dashboard)

All `/api/*` endpoints speak JSON and are unauthenticated by default — put the app
behind your auth proxy or add FastAPI middleware if you need lockdown.

### Campaigns

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/campaigns` | — | `{campaigns: [{id, name, status, total_voters, completed, pending, calling, dnc, failed, created_at}]}` |
| POST | `/api/campaigns` | `{name, script_intro, caller_id?, max_concurrent?, list_ids: [int], questions: [{prompt_text, answer_type, choices?, confirm}]}`. If `caller_id` is empty we fall back to `SIGNALWIRE_PHONE_NUMBER`. | `{ok, campaign_id}` |
| GET | `/api/campaigns/{id}` | — | `{campaign, questions, lists, voters, results}` |
| PATCH | `/api/campaigns/{id}` | any subset of `{name, script_intro, caller_id, max_concurrent, list_ids, questions}`. **Questions may only be changed while status=='draft'** — 409 otherwise. | `{ok}` |
| DELETE | `/api/campaigns/{id}` | — | `{ok}` |
| POST | `/api/campaigns/{id}/start` | — | `{ok, status: 'started'|'already_running'}` — **409** if the campaign is already `completed`. |
| POST | `/api/campaigns/{id}/pause` | — | `{ok, status: 'paused'}` |
| POST | `/api/campaigns/{id}/retry-failed` | — | `{ok, status, reset}` — flips `failed` + stuck `calling` voters back to `pending` and starts the dialer. Safe in production. |
| POST | `/api/campaigns/{id}/reset` | — | **DESTRUCTIVE**. `{ok, voters_reset, voters_dnc, calls_deleted, answers_deleted}` — wipes calls/answers and flips every non-DNC voter back to `pending`; campaign returns to `draft`. Intended for testing; UI requires explicit confirmation. |
| GET | `/api/campaigns/{id}/results` | — | per-question distribution with counts AND `percent` |
| GET | `/api/campaigns/{id}/export/answers.csv` | — | `text/csv`, one row per answer with voter + question metadata |
| GET | `/api/campaigns/{id}/export/voters.csv` | — | `text/csv`, voter roster for the campaign with dial status |

### Voter lists

Voters are organized into named lists (e.g. "Boston Women 25-45"). A campaign is
linked to one or more lists via `campaign_lists`; status per (campaign, voter) is
tracked in `campaign_voter_state`.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/lists` | — | `{lists: [{id, name, description, voter_count, male_count, female_count, other_count}]}` |
| POST | `/api/lists` | `{name, description?}` | `{ok, list_id}` |
| GET | `/api/lists/{id}` | — | `{list, voters, campaigns}` |
| PATCH | `/api/lists/{id}` | `{name?, description?}` | `{ok}` |
| DELETE | `/api/lists/{id}` | — | `{ok}` — cascades to voters and campaign_voter_state |
| POST | `/api/lists/{id}/voters` | either `{voters: [{phone, first_name?, last_name?, zip_code?, gender?, age_band?, party?}]}` or `{csv: "..."}`. CSV headers optional; default order is `phone,first_name,last_name,zip_code,gender,age_band,party`. | `{ok, added, skipped}` |

### Voters

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/voters/{id}` | — | voter row |
| PATCH | `/api/voters/{id}` | any subset of `{phone, first_name, last_name, zip_code, gender, age_band, party}` | `{ok}` |
| DELETE | `/api/voters/{id}` | — | `{ok}` |

`gender` is normalized to one of `M`, `F`, `NB`, `U` (or null).

### Reports

| Method | Path | Response |
|---|---|---|
| GET | `/api/reports/overview` | `{totals: {n_campaigns, n_lists, n_voters, n_calls, n_completed, n_dnc, n_failed, n_no_answer, n_answers, n_dnc_global}, campaigns: [{id, name, status, total_voters, completed, dnc, failed, answer_count, created_at}]}` |

#### Question shapes (for `POST /api/campaigns`)

The UI's preset picker emits one of these shapes. The backend just stores
`answer_type` and `choices_json`; the AI sees `choices` at runtime.

| `answer_type` | `choices` | Notes |
|---|---|---|
| `multi` | array of strings | AI submits one choice verbatim. |
| `yesno` | null | AI submits exactly `YES` or `NO`. |
| `scale` | null | AI submits integer 1–5. |
| `open`  | null | AI submits the full verbatim answer. |

Canonical preset choice-sets (matches `web/index.html` `QUESTION_PRESETS`):

- **Right direction / Wrong track** — `["Right direction","Wrong track","Not sure"]`
- **Top issue priority** — `["The economy and cost of living","Immigration and border security","Healthcare","Crime and public safety","Foreign policy and national security","Something else"]`
- **Approval 5-pt** — `["Strongly approve","Somewhat approve","Somewhat disapprove","Strongly disapprove","Unsure / no opinion"]`
- **Approval 4-pt** — `["Strongly approve","Somewhat approve","Somewhat disapprove","Strongly disapprove"]`
- **Outlook 5-pt** — `["Much better","Somewhat better","About the same","Somewhat worse","Much worse"]`
- **Trust 4-pt** — `["A great deal","A fair amount","Not very much","None at all"]`
- **Likert 5-pt** — `["Strongly agree","Somewhat agree","Neither agree nor disagree","Somewhat disagree","Strongly disagree"]`
- **Yes / No / Unsure** — `["Yes","No","Unsure"]`

### Calls

| Method | Path | Response |
|---|---|---|
| GET | `/api/calls/{call_id}` | `{call, voter, campaign, questions, answers}` snapshot |

### DNC

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/dnc` | — | `{dnc: [{phone, reason, source_call, created_at}]}` |
| POST | `/api/dnc` | `{phone, reason?}` | `{ok}` — global across campaigns |
| POST | `/api/dnc/bulk` | either `{entries:[{phone, reason?}]}` or `{csv:"phone,reason\n..."}` | `{ok, added, skipped}` |
| DELETE | `/api/dnc/{phone}` | — | `{ok}` |

### Metadata

| Method | Path | Response |
|---|---|---|
| GET | `/api/phone` | `{phone, display}` |
| GET | `/health` | SDK health check |
| GET | `/ready` | SDK readiness probe |

---

## Server-Sent Events (live updates without polling)

These streams close themselves when there's no more work to report — the client
EventSource stops reconnecting, so when a campaign is idle there is **zero**
ongoing traffic.

### `GET /api/calls/{call_id}/events`

Opens as soon as a user drills into a specific call. Events:

| Event | Payload | When |
|---|---|---|
| `snapshot` | full call snapshot | Immediately, so late-joiners see state |
| `consented` | `{call_id}` | After greeting |
| `declined` | `{call_id}` | Respondent declined to participate |
| `answer` | `{question_id, ordinal, prompt, value, skipped}` | Each answer recorded |
| `dnc` | `{phone, reason}` | Respondent asked to be removed |
| `reopt` | `{phone}` | Inbound caller removed themselves from DNC |
| `ended` | `{outcome, summary}` | Call ended — stream closes after this |

Keep-alive comments (`: keepalive\n\n`) fire every 30s to stop proxies closing
the stream prematurely.

### `GET /api/campaigns/{campaign_id}/events`

Opens on the campaign drilldown page. Events:

| Event | Payload | When |
|---|---|---|
| `snapshot` | `{campaign, voters, results}` | Immediately |
| `dial_placed` | `{voter_id, phone, call_id}` | Each outbound dial |
| `dial_failed` | `{voter_id, error}` | Dial failed (mark voter `failed`) |
| `voter_dnc` | `{voter_id, phone}` | Voter added to DNC mid-run |
| `voter_declined` | `{call_id}` | Respondent declined in-call |
| `call_ended` | `{call_id, outcome}` | A call ended |
| `progress` | `{call_id, answered_count}` | Answer recorded (good for live bars) |
| `idle` | `{campaign_id}` | No more `pending` or `calling` voters — **stream closes after this** |

---

## Static dashboard

| Path | What |
|---|---|
| `/` | Campaigns list (`web/index.html`) |
| `/campaign.html?id={id}` | Per-campaign drilldown with live SSE, edit dialog, CSV export buttons |
| `/call.html?id={call_id}` | Per-call live transcript |
| `/lists.html` | All voter lists with voter counts + gender breakdown |
| `/list.html?id={id}` | Voter roster for one list: CSV paste upload, quick-add row, inline edit per voter, demographic filter |
| `/reports.html` | Cross-campaign rollup — total calls, completion rate, DNC rate, per-campaign cards, CSV export links |
| `/dnc.html` | Global DNC registry management: filter by phone, manual add, CSV bulk add (`phone,reason`), remove single entry |
| `/sounds/typing.mp3` | SWAIG wait-file played while tools execute |

---

## End-to-end flow diagrams

### Outbound dial
```
Dashboard click Start
      ↓ POST /api/campaigns/42/start
PulseCall.dialer
      ↓ RestClient.calling.dial(url={base}/outbound?campaign_id=42&voter_id=17)
SignalWire places call
      ↓ caller answers
SignalWire fetches SWML
      ↓ GET/POST {base}/outbound?campaign_id=42&voter_id=17  (Basic Auth)
PulseCall /outbound → PollingAgent → ask_question loop
      ↓ each record_answer
      ↓   ├─ db.insert_answer(...)
      ↓   └─ events.publish("call:{call_id}","answer",...)
      ↓       └─> SSE stream to /api/calls/{call_id}/events subscribers
Call ends → on_summary → db.end_call + /api/calls/{id}/events emits 'ended'
```

### Inbound callback
```
Caller dials our SignalWire number
      ↓ SignalWire fetches SWML
      ↓ GET/POST {base}/inbound  (Basic Auth)
InboundAgent._per_call_config
      ↓ looks up caller_id_number
      ├─ in dnc_list → greet_inbound routes to wrap_up_dnc_confirmed or remove_from_dnc
      ├─ pending_voter → offers poll → ask_question loop
      ├─ completed_voter → offers DNC
      └─ unknown → explains who we are, offers DNC
```

### Inbound SMS STOP
```
Respondent texts "STOP" to our number
      ↓ SignalWire POSTs {base}/sms-webhook
PulseCall /sms-webhook → db.add_dnc(from_number, reason='inbound_stop')
      ↓ any future dial from any campaign is blocked by the DNC gate
```
