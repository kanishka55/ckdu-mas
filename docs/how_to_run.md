# How to run the CKDu MAS project (Windows, PowerShell)

Run the commands **in this order**. Each step says what you should see.
Tested on Windows 10 with Python 3.12.

---

## 1. First time only: get the code and install

```powershell
git clone https://github.com/kanishka55/ckdu-mas.git
cd ckdu-mas
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```
✅ Your prompt now starts with `(.venv)`.

If you already have the project, just update it instead:
```powershell
cd ckdu-mas
git pull
.venv\Scripts\activate
pip install -r requirements.txt
```

> **Every new terminal:** run `.venv\Scripts\activate` first, or VS Code may do it for you.
> If you see `(.venv)` at the start of the prompt, it is already active.

---

## 2. Check everything works (about 10 seconds)

```powershell
pytest
```
✅ `320 passed, 4 skipped`

Optional: run the full SPADE agent tests too (about 40 seconds):
```powershell
$env:CKDU_RUN_SPADE_ALERT="1"; $env:CKDU_RUN_SPADE_HOTSPOT="1"; $env:CKDU_RUN_SPADE_INTEGRATION="1"; $env:CKDU_RUN_SPADE_SIMULATION="1"
pytest
```
✅ `324 passed`

---

## 3. Quick demo: Contract Net, 1 coordinator and 3 villages (about 15 seconds)

```powershell
python -m src.sim.demo_contract_net --embedded-server
```
✅ `winners: ['Medawachchiya'] confirmed: ['Medawachchiya'] no reply: []`

---

## 4. Full simulation: all agents, 12 months (about 35 seconds)

```powershell
python -m src.sim.runner --end-month-index 11 --calendar-months 10,11,12,1,2,3,4,5,6,7,8,9 --output-dir C:\ckdu-runs\run-01 --resource-pool 3 --hotspot-trigger-tier Poor --hotspot-consecutive-months 2 --simulate-delivery-failure Talawa:0 --embedded-server
```
✅ Ends with a results table (`negotiation_rounds ... ok`). Results are saved in `C:\ckdu-runs\run-01`.

⚠️ **Use a new `--output-dir` every run** (`run-02`, `run-03`, …). The runner refuses a folder that
already has results in it.

What the options mean:

| Option | Meaning |
|---|---|
| `--end-month-index 11` + `--calendar-months 10,…,9` | 12 months, starting in October |
| `--resource-pool 3` | 3 RO units per month |
| `--hotspot-trigger-tier Poor --hotspot-consecutive-months 2` | hotspot = Poor or worse 2 months in a row |
| `--simulate-delivery-failure Talawa:0` | test case: Talawa cannot take its unit in month 0, so you see `failure` and a re-offer |
| `--embedded-server` | runs the XMPP server inside the program, so no second terminal is needed |

Try `--resource bowser` for water bowsers, or a different `--resource-pool`.

Look at the results:
```powershell
Import-Csv C:\ckdu-runs\run-01\logs\allocations.csv | Format-Table
Import-Csv C:\ckdu-runs\run-01\logs\evaluation_summary.csv | Format-Table
```

---

## 5. Dashboard (results in the browser)

```powershell
$env:CKDU_SIMULATION_RUN_DIR = "C:\ckdu-runs\run-01"
$env:CKDU_LOGS_DIR = "C:\ckdu-runs\run-01\logs"
streamlit run dashboard/app.py
```
✅ The browser opens at http://localhost:8501. If it doesn't, open that address yourself.

Use the folder from step 4 in both lines. Views at the top of the page:
- **Team outputs**: risk map, risk tiers, allocations, Contract Net vs baselines
- **Agent Monitor**: every agent and what it did
- **Live Coordination**: messages between agents, each month's rounds, winners
- **Simulation Lab**: start a new run from the browser

Stop the dashboard with `Ctrl+C` in the terminal.

---

## 6. Optional: two terminals (external XMPP server)

**Terminal 1**, leave it running:
```powershell
spade run --host localhost --memory
```
**Terminal 2**: run the same commands as steps 3 or 4, **without** `--embedded-server`
(and with a new `--output-dir`).

⚠️ Close `spade run` (`Ctrl+C`) before going back to `--embedded-server`, because both use port 5222.

---

## Other useful commands

| Command | What it does |
|---|---|
| `python -m src.sim.demo_well_monitor --embedded-server` | Part A demo: ticks → readings → villages |
| `python -m src.sim.demo_risk_scoring --embedded-server` | Part B demo: readings → risk scores |
| `python -m src.sim.demo_f1_f2 --embedded-server` | A + B together, all 11 villages answer a CFP |
| `python -m src.sim.validate_wqi` | our WQI tiers compared with Cooray et al. 2019 |
| `python -m src.sim.check_risk --village Padaviya --month 1` | WQI of one village, step by step |

---

## Problems and fixes

| You see | Fix |
|---|---|
| `source : The term 'source' is not recognized` | That is a bash command. In PowerShell use `.venv\Scripts\activate` |
| `running scripts is disabled on this system` (when activating) | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`, then activate again |
| `output directory is not empty; choose a fresh isolated path` | Use a new `--output-dir`, e.g. `run-02` |
| `refusing to write simulation output under protected path` | Put `--output-dir` outside the project, e.g. `C:\ckdu-runs\run-01` |
| Embedded run fails while `spade run` is open | Close `spade run` first (port 5222 is in use) |
| Dashboard says file not found in `F:\...\logs` | Set `CKDU_LOGS_DIR` (step 5) and restart the dashboard |
| `ModuleNotFoundError: No module named 'src'` | `git pull` (fixed in the dashboard), and run commands from the `ckdu-mas` folder |
| Anything else | Send the full error message to the team chat |

More detail: `docs/simulation_runner.md`, `docs/known_issues.md`, `CLAUDE.md`.
