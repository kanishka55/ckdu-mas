# CKDu Well-Water Safety Network (multi-agent system)

MSc Data Science and AI, University of Sri Jayewardenepura. Multi-Agent Systems coursework, team of 4.
Python only (SPADE). Not NetLogo, not Mesa.

## What we are building
A simulated multi-agent system that scores village well-water risk for CKDu-associated water-quality
indicators and negotiates scarce safe-water resources (RO units, water bowsers) between villages.
It is a RISK-INDICATOR tool built on published water-quality research. It never diagnoses CKDu and
never claims water chemistry causes CKDu (the science is contested). Keep that wording in code, UI and docs.

Full plan: docs/project_plan.md (read it when you need background, do not paste it into answers).

## Agents (SPADE, one process per agent)
- ClockAgent: broadcasts a simulated "new month" tick.
- WellMonitorAgent: ingests monthly well readings per village.
- RiskScoringAgent: computes a Water Quality Index (WQI) per well and a risk tier per village.
- HotspotAgent: compares village risk with published CKDu aggregate patterns (clustering/correlation).
- ResourceCoordinatorAgent: runs the FIPA Contract Net Protocol (CFP, propose/refuse, accept/reject,
  inform/failure) across VillageAgents. The resource pool must be genuinely scarce.
- VillageAgent (10-15 instances): holds local well data, computes a bid from need, replies to CFPs.
- AlertAgent: sends tiered, non-alarmist alerts (use another water source, seek screening; never a diagnosis).
Baselines to compare against: equal split and first-come-first-served allocation.

## Stack and commands
- Python 3.11 or 3.12, virtual environment in .venv
- spade (4.x has a built-in XMPP server: run `spade run` in its own terminal), pandas, numpy, scipy,
  scikit-learn, streamlit, folium, pytest
- Pin versions in requirements.txt after installing; do not guess version numbers.
- Commands (Claude: fill these in as they are created, keep them accurate):
  - setup: `py -3.12 -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements.txt`
  - start XMPP server: `spade run --host localhost --memory` (`--host` must match the agents' `@localhost`)
  - toy Contract Net demo: `python -m src.sim.demo_contract_net --embedded-server` (one process), or
    `python -m src.sim.demo_contract_net` with `spade run` in another terminal. External-server logins
    rely on the STARTTLS workaround in src/agents/xmpp_patch.py; see docs/known_issues.md.
    Never run both at once (port 5222).
  - risk scoring demo (stub WellMonitor -> RiskScoringAgent -> stub subscriber):
    `python -m src.sim.demo_risk_scoring --embedded-server` (writes logs/risk_scores.csv)
  - WQI tier distribution vs Cooray et al.: `python -m src.sim.validate_wqi`
  - check the risk score by hand: `python -m src.sim.check_risk --F 2.5 --hardness 300`
    (one well) or `python -m src.sim.check_risk --village Padaviya --month 1`
  - well monitor demo (ticks -> readings -> villages): `python -m src.sim.demo_well_monitor --embedded-server`
  - F1 -> F2 chain (real WellMonitor -> RiskScoring -> villages + F3 stand-in):
    `python -m src.sim.demo_f1_f2 --embedded-server` (writes logs/risk_scores.csv, logs/well_scores.csv)
  - run simulation (all agents, 12 months, fresh output dir each time):
    `python -m src.sim.runner --end-month-index 11 --calendar-months 10,11,12,1,2,3,4,5,6,7,8,9
    --output-dir <new dir> --resource-pool 3 --hotspot-trigger-tier Poor --hotspot-consecutive-months 2
    --simulate-delivery-failure Talawa:0 --embedded-server` (see docs/simulation_runner.md)
  - opt-in SPADE integration tests: set CKDU_RUN_SPADE_ALERT/HOTSPOT/INTEGRATION/SIMULATION=1, then `pytest`
  - tests: `pytest`
  - dashboard (shows one simulation run's results in the browser):
    1. run the simulation above with e.g. `--output-dir C:\ckdu-runs\run-01`
    2. `$env:CKDU_SIMULATION_RUN_DIR = "C:\ckdu-runs\run-01"` (the same folder; Agent Monitor and
       Live Coordination views) and `$env:CKDU_LOGS_DIR = "C:\ckdu-runs\run-01\logs"` (Team outputs
       view; without it that view reads the project's own logs/, which the runner never writes)
    3. `streamlit run dashboard/app.py`, then open http://localhost:8501
    Views at the top: Team outputs, Agent Monitor, Live Coordination, Simulation Lab (can start runs itself).
  - risk map (Function 2, animated): `python -m src.sim.demo_risk_scoring --embedded-server --months all`,
    then `streamlit run dashboard/risk_map.py`

## Layout
- data/raw: real values extracted from papers (see data/raw/README_data_sources.md). Treat as read-only.
- data/processed: derived files, including synthetic wells (always labelled).
- src/core: pure functions only (WQI, bidding, ontology/message schemas, allocation baselines). No SPADE imports.
- src/agents: thin SPADE agent classes that call src/core.
- src/sim: simulation runner and data loading.
- dashboard/: Streamlit app. It reads the log files; it does not run inside the agent process.
- logs/: message log (messages.csv) and allocation results.
- tests/: pytest.

## Rules
- Business logic lives in src/core and is unit-tested without XMPP.
- Every message between agents is logged (timestamp, sender, receiver, performative, thread id, payload).
  This feeds the evaluation metrics.
- Every receive() and every CFP deadline has a timeout. Never block forever.
- Use fixed random seeds for any simulation or synthetic data.
- Message bodies are JSON with a documented schema in src/core/ontology.py.
- Thresholds and weights come from data/raw/ckdu_02_wqi_method_and_thresholds.csv. If a value is
  `TO_FILL`, stop and ask; never invent a threshold or weight.
- Real vs synthetic: any synthetic row carries source="synthetic". Never mix silently.
- No individual patient data anywhere. Only published aggregates.
- Do not add NetLogo or Mesa. Do not add a framework without asking.

## Verification (do this before saying work is done)
- `pytest` passes.
- The toy Contract Net demo (1 coordinator, 3 villages) runs end to end and writes logs/messages.csv.
- Show the command you ran and its output, not just a claim that it works.

## Team workflow
- Per-member task briefs: docs/tasks/README.md (A: data, Clock + WellMonitor agents;
  B: risk/hotspot; C: Village + Coordinator agents, Contract Net; D: alerts/eval/dashboard). Overview and timeline: docs/team_split.md.
- Four people, one branch per person, small commits, pull before starting.
- Keep this file under 200 lines. Move detail to docs/.
