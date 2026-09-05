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
│          │            (Groq / Anthropic)            │            │
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
       ▼  (Server-Sent Events)
┌─────────────────────────────────────────────────────────────────┐
│                  React + Vite + Tailwind Dashboard              │
│   Metrics Cards  │  Transaction Table  │  Details Drawer        │
│   Shadow Ledger  │  Channel Bandit     │  Live Policy Editor     │
└─────────────────────────────────────────────────────────────────┘
```

## Key Features

| Feature | Details |
|---|---|
| **Deterministic Classifier** | O(1) error-code → 5 failure category mapping. No LLM on the critical path. |
| **LLM Rationale Layer** | Groq/Anthropic generates localized Hinglish outreach. 8s timeout → canned fallback. |
| **Post-Flight Safety Validator** | Amount invariant (hard block) + tone blocklist (sanitize). |
| **Issuer Health Radar** | Rolling BIN failure counter → extended backoff on bank spike ≥30 failures. |
| **4 Recovery Strategies** | Silent Retry, Payment Link, Mandate Renewal, Circuit Breaker. |
| **Shadow Ledger** | Live comparison vs. naive cron retry to visualize ROI. |
| **Adaptive Channel Bandit** | Epsilon-greedy learning for SMS/WhatsApp/Voice selection per segment. |
| **Live Policy Editor** | Adjust confidence gate and thresholds without redeployment. |
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
#   LLM_PROVIDER=groq
#   GROQ_API_KEY=gsk_...
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
# Expected: 96 passed, 0 failed
```

---

## Reproduce the 100-Record Benchmark

1. Ensure the backend is running (`uvicorn app.main:app --reload --port 8000`).
2. Open the dashboard (`http://localhost:5173`).
3. Click **Start Simulation** in the sidebar.
4. Watch 100 synthetic payment failures stream live through the triage pipeline.

**Expected results:**
- Total Batch Volume: ~₹5,40,000
- RevGuard Recovery Yield: ≥65% (~₹3,51,000+)
- Guardrail Adherence: 100%
- CAT_05 (fraud/dispute) and CAT_06 (max retries): 0% recovery

---

## Environment Variables Reference

### Backend (`.env`)
| Variable | Required | Description |
|---|---|---|
| `RAZORPAY_KEY_ID` | Yes | Razorpay API key ID (test mode: `rzp_test_...`) |
| `RAZORPAY_KEY_SECRET` | Yes | Razorpay API key secret |
| `RAZORPAY_WEBHOOK_SECRET` | Yes | Webhook signature validation secret |
| `LLM_PROVIDER` | Yes | `groq` or `anthropic` |
| `GROQ_API_KEY` | If using Groq | Groq Cloud API key |
| `ANTHROPIC_API_KEY` | If using Anthropic | Anthropic API key |
| `DATABASE_URL` | No | Empty = SQLite dev; `postgresql+asyncpg://...` = Postgres |
| `ALLOWED_ORIGINS` | No | Comma-separated list of allowed CORS origins (e.g., your Vercel URL) |
| `APP_ENV` | No | `development` (default) or `production` |
| `POLICY_FILE` | No | Path to policy YAML (default: `policy.yaml`) |

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
4. In the Render dashboard, add secrets: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `GROQ_API_KEY`, `ALLOWED_ORIGINS` (your Vercel URL).

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
│   ├── llm.py               # LLM rationale layer (Groq/Anthropic + canned fallback)
│   ├── validator.py         # Post-flight safety validator
│   ├── issuer_radar.py      # BIN failure counter + extended backoff
│   ├── triage.py            # Full pipeline orchestrator
│   ├── channels.py          # Mocked outreach dispatchers (SMS/WhatsApp/Voice)
│   ├── bandit.py            # Epsilon-greedy adaptive channel bandit
│   ├── shadow_ledger.py     # Do-Nothing Shadow Ledger for ROI comparison
│   ├── dataset.py           # Faker-based 100-record benchmark dataset
│   ├── sse.py               # Server-Sent Events broadcaster
│   ├── scheduler.py         # APScheduler singleton for delayed retries
│   └── routers/
│       ├── webhook.py       # POST /webhook/razorpay
│       ├── stream.py        # GET /stream (SSE)
│       ├── simulate.py      # POST /simulate (batch runner)
│       └── policy_api.py    # GET/POST /policy (live policy editor)
├── frontend/
│   ├── src/
│   │   ├── App.jsx                         # Sidebar layout
│   │   ├── store/useStore.js               # Zustand state + SSE connection
│   │   └── components/
│   │       ├── Header.jsx
│   │       ├── MetricsCards.jsx            # Shadow Ledger ROI cards
│   │       ├── TransactionTable.jsx        # Live transaction table
│   │       ├── TransactionDetailsDrawer.jsx # Slide-out detail + emulator
│   │       ├── ControlPanel.jsx            # Simulation + Live Policy Editor
│   │       └── BanditChart.jsx             # Channel bandit visualization
│   ├── vercel.json
│   └── package.json
├── tests/                   # 96 tests, all passing
├── policy.yaml              # Guardrail configuration file
├── render.yaml              # Infrastructure-as-code for Render
├── requirements.txt
└── runtime.txt              # python-3.11.9
```

---

## License

MIT — See `LICENSE` for details.
