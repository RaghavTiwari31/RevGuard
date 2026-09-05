# RevGuard — Autonomous Revenue Recovery Engine

> **Razorpay AI Buildathon 2026 · Track 03 · Team RevGuard**

RevGuard is a production-grade, AI-powered dunning engine that autonomously triages failed Razorpay payments and recovers revenue — without ever letting the LLM make an unguarded financial decision.

---

## Architecture

```
Razorpay Webhook
       │
       ▼
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Backend                          │
│                                                                 │
│  Pre-Flight Guardrails ──► Classifier ──► Issuer Health Radar  │
│          │                     │                   │            │
│          │                     ▼                   │            │
│          │              LLM Rationale Layer         │            │
│          │             (Groq / Gemini)              │            │
│          │                     │                   │            │
│          │                     ▼                   │            │
│          │          Post-Flight Safety Validator   │            │
│          │         (Amount Invariant + Tone Check) │            │
│          │                     │                   │            │
│          │                     ▼                   │            │
│          └──────────► Action Dispatcher ◄──────────┘            │
│                    (4 Recovery Strategies)                      │
│                              │                                  │
│                   SQLAlchemy 2.0 Async ORM                      │
│                   SQLite (dev) / PostgreSQL (prod)              │
└─────────────────────────────────────────────────────────────────┘
       │
       ├──────────────► Durable Retry Queue ──┐
       │                (DB-backed, rearmed   │
       │                 at every startup)    │
       │                                      │
       │◄─────────────────────────────────────┘
       │                 replay through the same pipeline,
       │                 until the retry cap escalates it
       │
       ▼  (Server-Sent Events + REST history)
┌─────────────────────────────────────────────────────────────────┐
│                  React + Vite + Tailwind Dashboard              │
│   Metrics Cards  │  Transaction Table  │  Details Drawer        │
│   Shadow Ledger  │  Channel Bandit     │  Live Policy Editor    │
│   Issuer Radar   │  Retry Queue        │  Strategy A/B          │
└─────────────────────────────────────────────────────────────────┘

Inbound:  POST /webhook/reply ──► classifier ──► freeze + cancel retries
```

## Key Features

| Feature | Details |
|---|---|
| **Deterministic Classifier** | O(1) error-code → 5 failure category mapping. No LLM on the critical path. |
| **LLM Rationale Layer** | Groq or Gemini generates localized Hinglish outreach. 8s timeout → canned fallback; runs without a key at all. |
| **Post-Flight Safety Validator** | Amount invariant (hard block) + tone blocklist (sanitize). |
| **Issuer Health Radar** | Rolling BIN failure counter → extended backoff on bank spike ≥30 failures. |
| **4 Recovery Strategies** | Silent Retry, Payment Link, Mandate Renewal, Circuit Breaker. |
| **Durable Retry Queue** | Retries are persisted and rearmed at startup, so they survive the free tier's idle spin-down. Each replay re-enters the full pipeline and the retry cap converts a repeatedly failing payment into an escalation. |
| **Inbound Reply Handling** | `POST /webhook/reply` classifies customer replies; a stop keyword freezes automation and cancels every armed retry. |
| **Trace History API** | `GET /traces` — paginated, filterable history plus a SQL rollup, so a page refresh does not lose the run. |
| **Issuer Health API** | `GET /issuers` — per-BIN failure pressure and live outage state from the radar. |
| **Strategy A/B Harness** | `POST /simulate/ab` runs both channel strategies over identical data and reports which actually won. |
| **Shadow Ledger** | Live comparison vs. naive cron retry to visualize ROI. |
| **Adaptive Channel Bandit** | Epsilon-greedy (decaying exploration) per failure category, choosing only among policy-eligible channels. Weights persist across restarts. **Off by default** — see [Which channel strategy wins](#which-channel-strategy-wins). |
| **Live Policy Editor** | Adjust confidence gate and thresholds without redeployment. Patches are validated as a whole and applied atomically — a rejected patch leaves the live policy untouched. |
| **Benchmark Harness** | 100-record Faker dataset processed through the live code path, streaming to dashboard. |

---

## Local Setup (Reproduce in < 15 minutes)

### Prerequisites
- Python 3.11.9 (`runtime.txt` pins this)
- Node.js 18+ (for the dashboard)
- A Razorpay test-mode account

### 1. Clone & Install (Backend)

```bash
git clone https://github.com/<your-org>/revguard.git
cd revguard

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env and fill in:
#   RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET
#   LLM_PROVIDER=groq          (or gemini)
#   GROQ_API_KEY=gsk_...       (or GEMINI_API_KEY=AIzaSy...)
#   Both may be left empty — the pipeline falls back to canned rationales.
#   DATABASE_URL=   (leave empty for SQLite in dev)
```

### 3. Start the Backend

```bash
uvicorn app.main:app --reload --port 8000
```

API docs available at `http://localhost:8000/docs`

### 4. Install & Start the Dashboard

```bash
cd frontend
npm install
npm run dev
```

Dashboard at `http://localhost:5173`

### 5. Run the Backend Test Suite

```bash
cd ..
pytest tests/ -v
# Expected: 181 passed, 0 failed
```

---

## Reproduce the 100-Record Benchmark

1. Ensure the backend is running (`uvicorn app.main:app --reload --port 8000`).
2. Open the dashboard (`http://localhost:5173`).
3. Click **Start Simulation** in the sidebar.
4. Watch 100 synthetic payment failures stream live through the triage pipeline.

**Expected results** (measured on seed 42):
- Total Batch Volume: ~₹5,37,450
- RevGuard Recovery Yield: ~76.8% (~₹4,13,000) vs. ~16.2% for the naive cron baseline
- Dispatch Cost: under ₹100 for the whole batch
- Guardrail Adherence: 100%
- CAT_05 (fraud/dispute) and CAT_06 (max retries): 0% recovery

### Which channel strategy wins

`POST /simulate/ab` runs the benchmark twice — once with the adaptive bandit,
once with deterministic cost-aware selection — over byte-identical records, so
the difference is attributable to channel selection alone.

Measured on seed 42:

| Mode | Bandit net | Rule net | Winner |
|---|---|---|---|
| Cold (first-ever run) | ₹4,38,043 | ₹4,58,777 | **Deterministic**, by ₹20,734 |
| Warm (bandit pre-trained) | ₹4,49,585 | ₹4,58,777 | **Deterministic**, by ₹9,192 |

The bandit loses, and the reason is structural rather than a defect: the
deterministic rule already encodes exactly what the reward model rewards — a
voice call pays for itself above the high-value threshold, WhatsApp otherwise —
so the bandit can only rediscover that, and it pays real outreach to explore
arms it will ultimately reject. Pre-training halves the gap but never closes it.

`enable_adaptive_channel_bandit` therefore defaults to **false**. The bandit is
fully implemented, persisted and tested; flip the flag and re-run `/simulate/ab`
to check the result against your own traffic, where a different cost structure
or a genuinely unknown channel mix could reverse it.

This is the harness doing its job. A comparison that could only ever confirm the
more sophisticated option would not be worth running.

---

## API Reference

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + DB probe (Render health check and keep-alive pinger) |
| `POST` | `/webhook` | Razorpay payment-failure webhook. HMAC-verified; **mandatory in production** |
| `POST` | `/webhook/reply` | Inbound customer SMS/WhatsApp reply. A stop keyword freezes automation |
| `GET` | `/stream` | Server-Sent Events for the live dashboard |
| `POST` | `/simulate` | Run the 100-record benchmark |
| `POST` | `/simulate/ab` | Run both channel strategies and compare (`?warm=true` pre-trains the bandit) |
| `GET` | `/simulate/status` | Current or last run, including the A/B result |
| `GET` | `/traces` | Paginated history. Filter by `category`, `action_type`, `outcome_status`, `event_id` |
| `GET` | `/traces/stats` | SQL rollup by category, action and channel |
| `GET` | `/traces/{trace_id}` | One trace plus every attempt recorded for its event |
| `GET` | `/issuers` | Issuer Health Radar — failure pressure and outage state per BIN |
| `GET` | `/issuers/{bin}` | One issuer in detail |
| `GET` | `/retries` | Durable retry queue, with counts by status |
| `POST` | `/retries/{id}/run` | Fire a pending retry now (bank-uptime delays are 20-45 min) |
| `POST` | `/retries/{id}/cancel` | Cancel a pending retry |
| `GET`/`POST` | `/policy`, `/policy/update`, `/policy/reset` | Live policy editor |

---

## Surviving the Free Tier

Render's free tier idles the service out after ~15 minutes and gives it an
ephemeral filesystem. Two kinds of state would silently evaporate, so neither
lives in process memory:

| State | Where it lives | Recovered by |
|---|---|---|
| Pending retries | `scheduled_retries` table | `retry_queue.rehydrate()` at startup — retries that came due while asleep run in a staggered catch-up burst rather than being lost |
| Learned bandit weights | `bandit_arm_stats` table | `bandit.load_state()` at startup; flushed periodically during a run and again at shutdown |

Idempotency locks expire (`idempotency_lock_ttl_minutes`) for the same reason: a
lock is claimed before triage runs, so a spin-down mid-pipeline must not leave a
permanent tombstone that makes the event un-processable forever.

The service must run with `--workers 1`. The scheduler, the SSE fan-out and the
bandit cache are all per-process; a second worker would arm duplicate retry
timers and split SSE subscribers so half the dashboard updates never arrive.

---

## Environment Variables Reference

### Backend (`.env`)
| Variable | Required | Description |
|---|---|---|
| `RAZORPAY_KEY_ID` | Yes | Razorpay API key ID (test mode: `rzp_test_...`) |
| `RAZORPAY_KEY_SECRET` | Yes | Razorpay API key secret |
| `RAZORPAY_WEBHOOK_SECRET` | Yes in prod | Webhook signature validation secret. Required whenever `APP_ENV=production` |
| `LLM_PROVIDER` | No | `groq` (default) or `gemini`. With no key set, the pipeline uses canned templates. |
| `GROQ_API_KEY` | If using Groq | Groq Cloud API key |
| `GEMINI_API_KEY` | If using Gemini | Google AI Studio API key |
| `DATABASE_URL` | No | Empty = SQLite dev; `postgresql+asyncpg://...` = Postgres |
| `ALLOWED_ORIGINS` | No | Comma-separated list of allowed CORS origins (e.g., your Vercel URL) |
| `APP_ENV` | No | `development` (default) or `production`. In production, `/webhook` returns **503 until `RAZORPAY_WEBHOOK_SECRET` is set** — an unauthenticated webhook would let anyone trigger real customer outreach |
| `POLICY_FILE` | No | Path to policy YAML (default: `policy.yaml`) |

### Policy file (`policy.yaml`)
| Key | Default | Description |
|---|---|---|
| `max_retry_attempts` | 3 | Circuit-breaker fires on the next attempt |
| `anti_spam_cooldown_hours` | 4 | Minimum gap between outreach attempts |
| `quiet_hours_start` / `quiet_hours_end` | 21:00 / 09:00 | TRAI no-outreach window |
| `min_confidence_for_autonomous_action` | 0.75 | Below this, escalate to a human |
| `voice_call_min_amount_inr` | 100 | Eligibility **floor** for voice — clearing it does not force a call |
| `enable_adaptive_channel_bandit` | **false** | `true` enables the bandit — see [Which channel strategy wins](#which-channel-strategy-wins) |
| `idempotency_lock_ttl_minutes` | 15 | How long an *incomplete* lock is held before another attempt may reclaim it |
| `enable_scheduled_retries` | true | Master switch for the durable retry queue |
| `retry_catchup_delay_seconds` | 20 | Stagger for retries that came due during downtime |
| `max_retry_delay_minutes` | 720 | Ceiling on how far ahead a retry may be scheduled |
| `channel_unit_cost_inr` | 0.20 / 0.40 / 2.50 | Per-message cost for SMS / WhatsApp / Voice |

### Frontend (`frontend/.env.local`)
| Variable | Required | Description |
|---|---|---|
| `VITE_API_BASE_URL` | No | Full URL of Render backend. Empty = use Vite dev proxy |

---

## Deployment

### Backend (Render)
1. Push to GitHub.
2. Create a new **Web Service** on Render, point to the repo root.
3. Render auto-detects `render.yaml` — it creates both the API service and a free PostgreSQL database.
4. In the Render dashboard, add secrets: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `GROQ_API_KEY` (or `GEMINI_API_KEY`), `ALLOWED_ORIGINS` (your Vercel URL).

### Frontend stack

React 18 + Vite + Tailwind. Typography is **Inter Variable**, bundled via
`@fontsource-variable/inter` rather than a CDN, so the dashboard renders
identically offline and makes no third-party request on the demo path. All
currency and metric figures use tabular figures so digits do not jitter as
live values update. Recharts is code-split behind `React.lazy`, keeping the
initial bundle at ~186 kB (~58 kB gzipped).

### Frontend (Vercel)
1. Import the GitHub repo into Vercel.
2. Set the **Root Directory** to `frontend`.
3. Add environment variable: `VITE_API_BASE_URL=https://<your-render-service>.onrender.com`
4. Deploy. `vercel.json` handles SPA routing automatically.

### Keep-Alive Pinger
Render free tier spins down after 15 minutes of inactivity. Set up a free pinger at [cron-job.org](https://cron-job.org) to `GET https://<your-render-service>.onrender.com/health` every 10 minutes.

---

## Project Structure

```
revguard/
├── app/
│   ├── main.py              # FastAPI entry point, CORS, health endpoint
│   ├── policy.py            # Policy-as-code loader (policy.yaml)
│   ├── db.py                # SQLAlchemy 2.0 async models + engine
│   ├── guardrails.py        # Pre-flight safety checks (idempotency, retry cap, etc.)
│   ├── classifier.py        # Deterministic failure category classifier
│   ├── llm.py               # LLM rationale layer (Groq/Gemini + canned fallback)
│   ├── validator.py         # Post-flight safety validator
│   ├── issuer_radar.py      # BIN failure counter + extended backoff
│   ├── triage.py            # Full pipeline orchestrator
│   ├── channels.py          # Mocked outreach dispatchers (SMS/WhatsApp/Voice)
│   ├── bandit.py            # Epsilon-greedy adaptive channel bandit
│   ├── shadow_ledger.py     # Do-Nothing Shadow Ledger for ROI comparison
│   ├── dataset.py           # Faker-based 100-record benchmark dataset
│   ├── sse.py               # Server-Sent Events broadcaster
│   ├── scheduler.py         # APScheduler singleton
│   ├── retry_queue.py       # Durable retry queue + startup rehydration
│   └── routers/
│       ├── webhook.py       # POST /webhook, POST /webhook/reply
│       ├── stream.py        # GET /stream (SSE)
│       ├── simulate.py      # POST /simulate, POST /simulate/ab
│       ├── traces.py        # GET /traces (history + stats)
│       ├── issuers.py       # GET /issuers (Issuer Health Radar)
│       ├── retries.py       # GET/POST /retries (queue control)
│       └── policy_api.py    # GET/POST /policy (live policy editor)
├── frontend/
│   ├── src/
│   │   ├── App.jsx                         # Sidebar layout
│   │   ├── store/useStore.js               # Zustand state + SSE connection
│   │   ├── lib/format.js                   # Shared INR/label formatting
│   │   └── components/
│   │       ├── Header.jsx
│   │       ├── MetricsCards.jsx            # Shadow Ledger ROI cards
│   │       ├── TransactionTable.jsx        # Live transaction table
│   │       ├── TransactionDetailsDrawer.jsx # Slide-out detail + emulator
│   │       ├── ControlPanel.jsx            # Simulation, policy, A/B, ops panels
│   │       ├── BanditChart.jsx             # Channel bandit visualization
│   │       ├── IssuerRadar.jsx             # Per-BIN health + outage state
│   │       ├── RetryQueue.jsx              # Durable retry queue control
│   │       └── AbComparison.jsx            # Strategy A/B result
│   ├── vercel.json
│   └── package.json
├── tests/                   # 181 tests, all passing
├── policy.yaml              # Guardrail configuration file
├── render.yaml              # Infrastructure-as-code for Render
├── requirements.txt
└── runtime.txt              # python-3.11.9
```

---

## License

MIT — See `LICENSE` for details.
