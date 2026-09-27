================================================================================
EventFlow AI
================================================================================

OVERVIEW
--------
EventFlow AI is a predictive event-flow orchestration system built for a
hackathon demo. It simulates a 70,000-person stadium event (National Cup
Final), continuously forecasting crowd congestion, predicting cascading
failures across the venue topology, certifying candidate interventions using
a game-theoretic equilibrium solver, and streaming results live to an
operator command centre UI.

The system runs a 30-second orchestration cycle:
  generator tick -> EnKF twin assimilation -> forecast -> risk scoring ->
  anomaly detection -> cascade prediction -> optimise & certify interventions
  -> persist -> broadcast WebSocket events

MOCK MODE (zero-setup, no backend required) lets the full frontend run from
pre-generated JSON fixtures that validate against the same schemas the backend
uses — nothing is hand-authored fake data.

--------------------------------------------------------------------------------
PROJECT STRUCTURE
--------------------------------------------------------------------------------

EventFlow-AI/
  Backend/              FastAPI backend, orchestration engine, ML registry
    app/
      api/              REST routes (routes.py) and WebSocket (ws_routes.py)
      db/               SQLAlchemy models, SQLite, seed data
      ml_reference/     Deterministic reference implementations for all ML
      services/         Engine, state store, Commander, attendee, metrics
      config.py         Config loader
      schemas.py        Pydantic schemas (contract-enforced wire types)
    scripts/            Schema + mock exporters
    tests/              31 pytest tests
    config.yaml         Runtime knobs (cycle_sec, budgets, model params)
    run.py              Dev entry point (python run.py)
    requirements.txt    Python dependencies
    .env.example        Optional environment variable overrides
    eventflow.db        SQLite database (auto-created on first run)

  Frontend/             Vite + React 18 operator UI
    src/
      routes/
        CommandCentre.jsx   Main operator view (/)
        Attendee.jsx        Mobile attendee PWA (/attendee)
        Metrics.jsx         KPI judging panel (/metrics)
      components/
        TopBar.jsx          Header: sim clock, risk band, entity counts
        MapCanvas.jsx       deck.gl interactive venue map
        CommanderBar.jsx    Natural-language commander (right sidebar)
        WhatIfPanel.jsx     What-If scenario simulator
        PressureTimeline.jsx  Bottom panel: ranked time-to-critical list
        InterventionQueue.jsx Bottom panel: approve/reject recommendations
        TwinFidelityGauge.jsx Bottom panel: Digital Twin RMSE drift chart
        EntityDetailPanel.jsx Click-through entity detail + cascade overlay
        ActionHUD.jsx       Approved-action tracking overlay on map
        InterventionCard.jsx Card component for intervention items
        Toasts.jsx          Toast notification system
      lib/
        api.js            Single HTTP/mock network layer
        mocks.js          Mock driver (replays pre-generated JSON)
        mockLifecycle.js  Per-frame replay logic
        ws.js             WebSocket client (reconnect, resync, backpressure)
        format.js         Formatting utilities (clock, pct, decimals)
        useActionTracking.js  Approved-action lifecycle hook
      store/
        useStore.js       Zustand global store (single source of truth)
      mocks/              Pre-generated JSON fixtures (validated by CI)
    package.json
    vite.config.js

  ML/
    cascade.py            HX-Cascade GNN loader + per-entity risk (node_risk),
                          with an out-of-distribution guard; builds no cascades
    forecast_correction.py  LightGBM quantile correction of the twin forecast
    manifest.py           Model bundle manifest (sha256 binding) helpers
    models/, features/, training/, evaluation/, data/, kaggle/
                          v3 network, feature builder, training, evaluation,
                          map randomiser, Kaggle notebooks
    artifacts/hx_cascade_v3/  Cascade bundle in use (config cascade.gnn_artifact),
                          evaluated on held-out random maps
    artifacts/hx_cascade_v2/  Previous cascade bundle (comparison only)
    artifacts/forecast_correction_v2/  Forecast-correction bundle in use
    evaluation/results/   Swap-gate and forecast-gate evidence

  contracts/
    schemas/              JSON Schemas auto-exported from backend Pydantic models

  00_SHARED_CONTRACT.md   Wire format conventions (snake_case, units, types)
  01_BACKEND_CONTRACT.md  Backend API and cycle contract
  02_FRONTEND_CONTRACT.md Frontend component and store contract
  03_ML_CONTRACT.md       ML module interface contract
  RUNNING.md              Detailed run instructions and architecture notes

--------------------------------------------------------------------------------
MAIN FEATURES
--------------------------------------------------------------------------------

Command Centre (/)
  The main operator view. Composed of:
  - Interactive venue map (deck.gl) with entity risk colouring, glow halos,
    cascade path overlays, action rings, and selection highlights.
  - Entity Detail Panel: click any map node to see risk breakdown, forecast,
    and cascade propagation for that entity.
  - Action HUD: tracks approved interventions with live status updates.
  - Commander (right sidebar): natural-language Q&A backed by real tool calls
    against live state. Answers every question with a Sources tray showing the
    exact tool calls and digests that grounded the response.
  - What-If Panel (right sidebar): simulate a scenario (e.g. close Gate 3 for
    30 minutes) and see forecast impact before committing.
  - Pressure Timeline (bottom): ranked list of entities by time-to-critical,
    with hero highlighting for the most urgent entity. Click to see cascade.
  - Intervention Queue (bottom): recommended crowd-management actions with
    approve / reject buttons. Approving is real — it applies a demand-side
    relief factor to the simulation engine and forks a counterfactual branch
    in the Digital Twin for later regret measurement.
  - Digital Twin (bottom, collapsed by default): RMSE reduction gauge showing
    Ensemble Kalman Filter assimilation vs. uncorrected agent-based model.
    Drift mode toggle shows live divergence chart (red vs. green lines).

Attendee PWA (/attendee)
  Mobile-viewport personal assistant for event attendees. Shows:
  - Journey risk card (risk score and band for their planned route)
  - Smart route recommendations (primary and alternate paths ranked by risk)
  - Personalised nudge card (actionable suggestion from the operator, e.g.
    "Use Gate 7 instead — 12 min earlier with less crowd")
  - Accept / Decline response to nudges feeds compliance data back to the
    equilibrium solver

Metrics / Judging Panel (/metrics)
  Full-screen KPI panel showing:
  - Prediction accuracy: cascade precision, recall, and F1 (measured online
    against actual outcomes)
  - Twin fidelity: RMSE improvement %, ensemble size, assimilation spread
  - Decision quality: certificate accuracy, convergence rate, regret ledger
    (realised vs. counterfactual relief, comparing against do-nothing branch)

Simulation / Mock Mode
  By default (no .env file needed), the frontend runs entirely in mock mode:
  it replays a 90-frame pre-generated recording that loops continuously.
  Each loop iteration resets transient state (interventions, cascades, etc.)
  so the calm opening -> cascade alarm -> intervention cycle repeats cleanly.
  The mock data was exported by the real backend and validates against the
  same JSON Schemas used by the backend Pydantic models.

  To use live backend mode, see the "Full Stack" instructions below.

Cycle behaviour
  Each 30-second cycle (wall-clock, configurable via speed_multiplier):
  - Simulated time advances by one step in the seeded generator
  - EnKF twin assimilates current crowd density observations
  - Forecaster projects entity utilisation at 900/1800/3600s horizons
  - Risk scorer bands every entity (low/moderate/high/critical)
  - Anomaly detector flags z-score outliers
  - Deterministic flow cascade (services/cascade_flow.py) marks paths; the
    HX-Cascade GNN runs alongside in shadow mode (cascade.gnn_mode, see
    RUNNING.md) and does not change what is published
  - If any entity is predicted critical within 3600s:
      optimiser generates up to 5 candidate interventions
      equilibrium solver certifies each (STABLE / UNSTABLE verdict)
      certified interventions are queued for operator approval
  - Persist to SQLite, broadcast WebSocket events to all connected clients

--------------------------------------------------------------------------------
TECHNOLOGIES
--------------------------------------------------------------------------------

Frontend
  - React 18.3
  - Vite 6 (build tool and dev server)
  - React Router 6 (client-side routing)
  - Zustand 5 (global state store)
  - deck.gl 9 (WebGL venue map rendering)
  - Recharts 2 (Digital Twin RMSE line chart)
  - Tailwind CSS 3 (utility-class styling)
  - AJV 8 (JSON Schema validation for mocks, dev-time only)

Backend
  - Python 3.11+
  - FastAPI 0.115 (REST API + CORS)
  - Uvicorn 0.34 (ASGI server)
  - Pydantic 2.10 (schema validation, wire contract enforcement)
  - SQLAlchemy 2.0 (ORM)
  - SQLite (default database, zero setup)
  - PyYAML 6 (config.yaml loader)
  - NumPy 2.2 (simulation arithmetic, EnKF)
  - httpx 0.28 (async HTTP for local LLM, optional)
  - websockets 14 (WS broadcast)
  - python-dotenv 1.0 (.env file loader)
  - Optional (requirements-ml.txt): PyTorch 2.10 + torch_geometric 2.8
    (HX-Cascade GNN inference, CPU), LightGBM 4.6 (forecast correction)
  - pytest 8.3 (31 backend tests)

Database
  SQLite, stored at Backend/eventflow.db (auto-created on first run).
  Run tables (entity_state, risk_state, forecast) are cleared on every server
  start and on POST /demo/control {action: reset} so that a restart against a
  leftover database does not collide on (entity_id, sim_time).
  Persistent tables (intervention, certificate, regret_entry, nudge, audit_log)
  survive restarts.
  Optional: point DATABASE_URL at PostgreSQL by installing psycopg[binary] and
  setting the env var (see .env.example).

--------------------------------------------------------------------------------
ENVIRONMENT VARIABLES (.env.example)
--------------------------------------------------------------------------------

All values are optional. The backend runs with zero configuration.

  DATABASE_URL=sqlite:///./eventflow.db
      Default SQLite path. Change to postgresql+psycopg://... for Postgres.

  EVENTFLOW_CONFIG=config.yaml
      Path to the runtime config file. Defaults to Backend/config.yaml.

  # LOCAL_LLM_URL=http://localhost:11434/api/generate
      Optional Ollama endpoint. When set, the Commander uses a local LLM for
      phrasing instead of the built-in deterministic templates. Grounding
      validation runs identically either way.

  # LOCAL_LLM_MODEL=llama3.2
      Model name for Ollama (used only when LOCAL_LLM_URL is set).

  # REDIS_URL=redis://localhost:6379/0
      Optional Redis cache. Falls back to in-process cache by default.

The frontend has one variable:
  VITE_MOCK=0
      Set to exactly '0' to enable live mode (proxies /api and /ws to :8000).
      Any other value (or absent) = mock mode (no backend required).

--------------------------------------------------------------------------------
INSTALLATION
--------------------------------------------------------------------------------

Prerequisites: Node.js 18+, Python 3.11+

Backend setup:
  cd Backend
  pip install -r requirements.txt       # runs the whole product
  pip install -r requirements-ml.txt    # optional: the trained models
  (Linux: install the CPU torch wheel first, or pip pulls the CUDA build:
   pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu)

Frontend setup:
  cd Frontend
  npm install

--------------------------------------------------------------------------------
STARTING THE APPLICATION
--------------------------------------------------------------------------------

OPTION A — Frontend only (mock mode, no backend needed)
  cd Frontend
  npm run dev

  Open: http://localhost:5173  (or the URL printed by Vite)
  The entire application runs from pre-generated JSON fixtures.

OPTION B — Full stack (real backend + real 30-second orchestration cycle)

  Terminal 1 (Backend):
    cd Backend
    python run.py
    Server: http://localhost:8000
    API docs: http://localhost:8000/docs
    WebSocket: ws://localhost:8000/ws

  Terminal 2 (Frontend, live mode):
    cd Frontend
    npm run dev:live
    Open the URL printed by Vite (typically http://localhost:5173)

  Note: npm run dev:live uses --mode live which sets VITE_MOCK=0, proxying
  /api and /ws to localhost:8000. Do not use npm run dev for live mode — that
  stays in mock mode regardless of any .env file.

Demo speed-up (live mode only):
  curl -s -X POST http://localhost:8000/api/v1/demo/control \
    -H 'Content-Type: application/json' \
    -d '{"speed_multiplier": 120}'

Demo reset (live mode only):
  curl -s -X POST http://localhost:8000/api/v1/demo/control \
    -H 'Content-Type: application/json' \
    -d '{"action": "reset"}'

--------------------------------------------------------------------------------
URLS AND PORTS
--------------------------------------------------------------------------------

  Frontend dev server:     http://localhost:5173   (Vite default)
  Backend REST API:        http://localhost:8000/api/v1
  Backend API docs:        http://localhost:8000/docs
  Backend WebSocket:       ws://localhost:8000/ws
  Frontend routes:
    /             Command Centre (operator view)
    /attendee     Attendee PWA (mobile view)
    /metrics      Metrics / Judging Panel

--------------------------------------------------------------------------------
RUNNING TESTS
--------------------------------------------------------------------------------

Backend (76 tests):
  cd Backend
  python -m pytest tests/ -q

Frontend mock validation (17 mocks):
  cd Frontend
  npm run validate:mocks

Frontend build check:
  cd Frontend
  npm run build

--------------------------------------------------------------------------------
TROUBLESHOOTING
--------------------------------------------------------------------------------

"App loads but shows error toasts / blank panels"
  If using npm run dev, the app is in mock mode and should work without a
  backend. Check the browser console for errors.
  If using npm run dev:live, make sure the backend (python run.py) is running
  at localhost:8000 first.

"Log warning: ml.cascade found at ML/cascade.py but failed to import"
  PyTorch 2.10 and torch_geometric 2.8 are needed for the HX-Cascade GNN.
  Without them the backend still starts and runs; /health then reports
  cascade gnn_mode "off". Install them (with LightGBM for the forecast
  correction):
    pip install -r requirements-ml.txt
  These are CPU-only; no GPU or CUDA is required.

"First 30-45 seconds show no interventions (live mode)"
  This is expected. The forecaster warm-up period and entity ramp from calm to
  critical is intentional. Use the speed_multiplier demo control to skip ahead:
    curl -X POST http://localhost:8000/api/v1/demo/control \
      -H 'Content-Type: application/json' -d '{"speed_multiplier": 120}'

"cascade / twin / forecast returns MODEL_NOT_READY"
  The backend is still in its warm-up phase. Wait 1-2 cycles (30-60 seconds)
  and retry. These errors are suppressed in the UI and are not a bug.

"I want to use PostgreSQL instead of SQLite"
  Install:  pip install psycopg[binary]
  Set in Backend/.env:  DATABASE_URL=postgresql+psycopg://user:pass@host/db
  Copy .env.example to .env and fill in your connection string.

"Frontend mock validation fails (npm run validate:mocks)"
  The mock JSON files in Frontend/src/mocks/ have drifted from the backend
  schemas. Regenerate them:
    cd Backend
    python -m scripts.export_schemas --out ../contracts/schemas
    python -m scripts.export_mocks   --out ../Frontend/src/mocks --cycles 160
    cd ../Frontend && npm run validate:mocks

"I want to use a local LLM for the Commander"
  Install and start Ollama (https://ollama.com), pull a model (e.g. llama3.2),
  then set in Backend/.env:
    LOCAL_LLM_URL=http://localhost:11434/api/generate
    LOCAL_LLM_MODEL=llama3.2
  The grounding validator runs identically; only the phrasing changes.

--------------------------------------------------------------------------------
