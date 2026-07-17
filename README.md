# QI Spine Voice Receptionist (Bolna)

Voice AI front desk for a **real multi-branch clinic** (QI Spine Clinic, Guwahati - public listings for Rajouri Garden, Guwahati & Gurugram). Callers speak English, Hindi, or mid-call Hinglish and leave with a booked / rescheduled / cancelled appointment - no human in the loop for the happy scheduling path.

## Stack choice: Bolna

| Criterion | Why Bolna won for this clinic |
|---|---|
| **Multilingual + code-switch** | India-native stack with first-class Hindi / Hinglish. Retell supports Hindi as a language, but mid-utterance code-switching is weaker for the exact failure mode this brief requires. |

## What we built

### Agent (Bolna)
- Full lifecycle: book / reschedule / cancel / conflict recovery
- Two branches, multiple practitioners, specialty triage
- EN / HI / code-switch via multilingual ASR+LLM+TTS - **no hardcoded translation table**
- Prompt designed to never re-ask known facts; every turn moves toward completion

### Backend
- **Cliniko PMS write-back** (real): confirmed patients + appointments are mirrored into Cliniko. Idempotent keys, defined failure behavior (local book succeeds, sync marked `failed` for retry - patient is not told the booking failed).
- Local DB stays the source of truth for availability during a live call; Cliniko is the PMS of record. Base URL is auto-derived from the API key shard; run `python -m scripts.cliniko_provision` once to map businesses/practitioners/appointment types.
- Call-state store for **drop recovery**, **missed-outbound callbacks**, family-line disambiguation

### Eval harness
- Multi-turn scripted scenarios
- Metrics **per language** (en / hi / mixed)
- Turns-to-completion, redundant-question rate, availability freshness, earliest cross-branch
- Backend tool latency percentiles; ASR/LLM/TTS via optional Bolna export

## Clinic data (sourced, not invented)
Public QI Spine listings adapted into Cliniko-shaped entities:

| Branch | Code | Notes |
|---|---|---|
| QI Spine - Rajouri Garden, New Delhi | `rajouri_garden` | Physio: Dr. Isha Ghelani, Dr. Shital Gaikwad |
| QI Spine - Gurugram Sector 43 | `gurugram` | Physio: Dr. Disha Ashar, Dr. Nidhi Sanghvi Shah; Spine/Ortho: Dr. Gautam Shetty |
| QI Spine - Guwahati (near LGB International Airport, Assam) | `guwahati` | Physio: Dr. Anjali Das, Dr. Rituraj Kalita; Spine/Ortho: Dr. Nirav Deka |

Sources: [qispine.com Delhi NCR](https://www.qispine.com/locate-us/spine-clinic-in-delhi-ncr), [Gurugram](https://www.qispine.com/locate-us/spine-clinic-in-gurugram). Hours/slots are operationalized for the demo scheduler (Mon–Sat 8–8, Sun 9–5, Asia/Kolkata, 15‑min buffer).

Seeded test phones:
- `9876543210` - Priya Sharma (returning)
- `9988776655` - Neha + Rohan Kapoor (family line)
- `9876501234` - Amit Verma (outbound callback demos)

## Architecture

```
Caller ⇄ Bolna (ASR · LLM · TTS · telephony)
            │ custom function tools
            ▼
     FastAPI /tools/*  ──► Postgres / SQLite (appointments, patients, call_sessions)
            │
            ▼
     Cliniko PMS write-back (real)
```

### Required scenario → mechanism

| Scenario | Mechanism |
|---|---|
| Returning patient | `start_call` → `patient_lookup` by phone + recent appointments |
| Missed outbound callback | `simulate_missed_outbound` / outbound webhook → `resume_mode=outbound_callback` |
| Stale availability | Prompt + tool contract: re-call `check_availability`; response includes `queried_at` + `force_fresh` |
| Earliest across branches | `same_day` + `earliest_only` with **no** branch filter; search merges all practitioners |
| Branch specialty triage | `branch_code` + `department_code`/`specialty` filters |
| Dropped call recovery | session `status=dropped` within TTL → ack + restore `saved_context` |

## Quick start

### Prerequisites
- Python 3.12+
- Bolna account (agent created in dashboard; tools point at your public backend URL)
- Cliniko API key for PMS write-back

### 1. Configure

```bash
cp .env.example .env
# set API_KEY, CLINIKO_API_KEY, and later PUBLIC_BASE_URL (ngrok)
```

### 2. Run backend
**Local SQLite quickstart:**

```bash
cd backend
pip install -r requirements.txt
# ensure .env has sqlite DATABASE_URL (default in .env.example)
set PYTHONPATH=.
python -m scripts.seed
python -m scripts.cliniko_provision
uvicorn app.main:app --reload --port 8000
```

API: http://localhost:8000/docs  
Health: http://localhost:8000/webhooks/health

### 3. Expose publicly (for Bolna tools)

```bash
ngrok http 8000
# set PUBLIC_BASE_URL=https://xxxx.ngrok-free.app in .env
```

### 4. Wire Bolna agent tools

Create/configure the agent in the Bolna dashboard (prompts + tool URLs pointing at `PUBLIC_BASE_URL`).  

### 5. Run eval harness

```bash
pip install -r backend/requirements.txt
python eval/harness.py --base-url http://localhost:8000 --api-key $API_KEY
```

Reports: `eval/reports/latest.json`

## Tool schema (agent ↔ backend)

All tools require header `X-API-Key: <API_KEY>`.

| Tool | Method | Purpose |
|---|---|---|
| `/tools/start_call` | POST | Identity, resume mode, patient lookup |
| `/tools/check_availability` | POST | Live slots (always fresh) |
| `/tools/ensure_patient` | POST | Create/fetch patient by phone+name |
| `/tools/book_appointment` | POST | Conflict-checked book + EHR write-back |
| `/tools/reschedule_appointment` | POST | Reschedule + conditional fee |
| `/tools/cancel_appointment` | POST | Cancel + conditional fee |
| `/tools/update_call_context` | POST | Persist facts for drop recovery |
| `/tools/create_follow_up` | POST | Human/clinical escalation ticket |
| `/tools/clinic_directory` | GET | Branches, doctors, policies |

Prompt + logic: `docs/PROMPT_LOGIC.md`

### Cliniko setup

1. Put your API key in `.env` as `CLINIKO_API_KEY` (base URL is derived from the key's shard suffix automatically).  
2. From `backend/`: `python -m scripts.cliniko_provision` to map businesses / practitioners / appointment types into the local DB.  
3. Practitioners can't be created via the Cliniko API - create any unmatched ones in the Cliniko UI, then re-run the script.  

## Go-live checklist (no phone number purchase)

1. local SQLite + `uvicorn`  
2. Expose backend: `ngrok http 8000` → set `PUBLIC_BASE_URL` in `.env`  
3. Wire Bolna Tools tab to `PUBLIC_BASE_URL`  
4. Bolna dashboard → your agent → **Get call from agent** (outbound to your mobile) 
5. Confirm bookings appear in Cliniko after `python -m scripts.cliniko_provision`  

Optional helpers:
- Outbound to your phone: `python scripts/bolna_start_call.py --phone 9999999999 --name "Chinmoy Das"`

## Known limitations

- Cliniko practitioner ids must be mapped via `scripts.cliniko_provision` after signup (practitioners can't be API-created); unmapped entities cause write-back to be marked `failed` while the local booking still succeeds  
- Harness validates tool/state correctness; barge-in / ASR noise need live listening  
- ALL CAPS names are normalized for speech via `display_name` / `natural_name` - TTS still depends on the chosen voice model  
- Same-day “earliest” depends on seed schedules and existing bookings at eval time  
### Demo 
- You can hear the recordings from demo directory
### Bolna Agent
- [Here you can find](https://bolna.ai/a/b43e66c9-5902-4aec-9741-bcac4fa94b02)
