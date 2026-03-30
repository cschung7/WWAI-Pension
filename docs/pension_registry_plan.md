# Pension ETF Registry — Implementation Plan
**Project**: WWAI-Pension (`pension.wwai.app`, port 8121)
**Date**: 2026-03-30
**Status**: PLANNING

---

## Problem Statement

The WWAI-ETF-Fabless system has trained 731 ETF runs stored in `portfolio.db` (SQLite). Among these, ~20 are FSC-compliant pension strategies covering different risk-return appetites. Pension clients have heterogeneous demands:

- **안심형 (T1)**: CAGR ≥7%, MaxDD ≤-10% — capital preservation
- **균형형 (T2)**: CAGR ≥15%, MaxDD ≤-18% — balanced
- **성장형 (T3)**: CAGR ≥25%, MaxDD ≤-25% — growth
- **적극형 (T4)**: CAGR ≥35% — aggressive

Currently there is no way to browse, filter, or compare these strategies. We need a registry dashboard + scatter chart at `pension.wwai.app` mirroring what `etf-fabless.com/registry` does for ETF operators, but enhanced for IRP/DC pension context.

---

## Architecture Decision

### Single Harness Already Exists

The ETF-Fabless `run_backtest(db_tags=[...])` → `PortfolioDB.record_run(tags=)` path writes to `portfolio.db`'s `tags TEXT DEFAULT '[]'` column. No new DB or schema migration needed.

```
Experiment .py
  → run_backtest(db_tags=["pension","fsc_compliant","tier:T2","bond_pct:30"])
  → PortfolioDB.record_run(...)
  → portfolio.db runs.tags
  → pension_registry.py reads via sqlite3 (read-only URI)
  → /registry/data JSON API
  → registry.html / scatter.html (Plotly.js)
```

### DB Access Pattern

```python
# Read-only, no lock contention with ETF-Fabless writer
conn = sqlite3.connect(
    "file:///mnt/nas/WWAI/WWAI-ETF-FABLESS/etf-fabless-framework/results/portfolio.db?mode=ro",
    uri=True
)
```

Filter: `WHERE json_extract(tags, '$') LIKE '%pension%'`

---

## Components (6 pieces)

### 1. `scripts/pension_registry_tagger.py`
One-time script to UPDATE existing ~20 pension runs with tags.

**Input**: query `runs WHERE product LIKE 'Pension-%' OR product LIKE '%semiai%defense%'`
**Action**: UPDATE tags JSON with `["pension","fsc_compliant","tier:T?","bond_pct:30","strategy:s35ib"]`
**Risk**: Writes to portfolio.db — run once, safe (idempotent via overwrite)

Tag schema:
```json
["pension", "fsc_compliant", "tier:T2", "bond_pct:30", "strategy:s35ib", "regime:semiai_defense"]
```

### 2. `api/pension_registry.py` — FastAPI router
Endpoints:
- `GET /registry/data` → list of runs filtered by `pension` tag, returns metrics array
- `GET /registry/{run_id}/equity` → equity curve for modal
- `GET /registry/summary` → tier counts, best-per-tier, scatter scatter data

Response schema per run:
```json
{
  "run_id": "abc123",
  "product": "Pension-SemiAI-Defense-S3.5+IB",
  "cagr": 25.7,
  "maxdd": -15.88,
  "calmar": 1.618,
  "sharpe": 1.428,
  "turnover": 42.1,
  "bond_pct": 30.0,
  "fsc_compliant": true,
  "tier": "T2",
  "tier_label": "균형형",
  "tags": ["pension","fsc_compliant","tier:T2","bond_pct:30"],
  "backtest_start": "2022-01-01",
  "backtest_end": "2026-03-30",
  "period_years": 4.2
}
```

### 3. `static/registry.html`
Registry table UI:
- **Filter bar**: Tier chips (안심/균형/성장/적극형), FSC-only toggle, Min CAGR / Max DD sliders
- **"나의 전략 찾기" configurator**: User inputs target return + max loss tolerance → highlights matching tier
- **Table columns**: Strategy | Tier | CAGR | MaxDD | Calmar | SR | Turnover | Bond% | FSC | Period | View
- **Row click** → equity curve modal (Plotly.js line chart)
- **Sort**: by any column header

### 4. `static/scatter.html`
Plotly.js bubble scatter dashboard:
- **X-axis**: MaxDD (%) — more negative = right
- **Y-axis**: CAGR (%)
- **Bubble size**: Calmar ratio
- **Color**: Tier (T1=green, T2=blue, T3=orange, T4=red)
- **Annotations**: FSC badge, benchmark lines (CAGR=7%, DD=-10%, CAGR=15%, DD=-18%)
- **Hover**: full metric card
- **Click**: opens same equity curve modal

### 5. Wire into `main.py`
```python
from api.pension_registry import router as registry_router
app.include_router(registry_router, prefix="/registry")
app.mount("/registry", StaticFiles(directory="static/registry"), name="registry")

@app.get("/strategy-map")
async def strategy_map(): return FileResponse("static/scatter.html")
```

New navbar items: "전략 지도" (scatter) and "전략 찾기" (registry table)

### 6. Tag Future Experiments
In `mode3_pension_s35_fsc.py` and all future pension experiment calls:
```python
run_backtest(
    ...,
    db_tags=["pension", "fsc_compliant", "tier:T2", "bond_pct:30", "strategy:s35ib"]
)
```

---

## File Layout

```
WWAI-Pension/
├── api/
│   ├── __init__.py
│   ├── pension_router.py        (existing)
│   └── pension_registry.py      (NEW)
├── static/
│   ├── index.html               (existing wizard)
│   ├── frontier.html            (existing)
│   ├── registry.html            (NEW — table)
│   └── scatter.html             (NEW — bubble chart)
├── scripts/
│   └── pension_registry_tagger.py  (NEW — one-time tag script)
├── main.py                      (MODIFY — add routes)
├── products/
│   └── wwai-semiai-defense-rotation/
│       ├── metrics.json         (done ✅)
│       ├── pitch.md             (done ✅)
│       └── slides.html          (done ✅)
└── docs/
    └── pension_registry_plan.md  (this file)
```

---

## Data Flow

```
portfolio.db (ETF-Fabless NAS path)
  │  [read-only sqlite3 URI]
  ▼
pension_registry.py
  ├── parse tags JSON
  ├── filter pension tag
  ├── extract tier from tags
  └── return sorted metrics list
        │
        ├─→ /registry/data → registry.html (table)
        ├─→ /registry/{id}/equity → equity modal
        └─→ /registry/summary → scatter.html (Plotly bubble)
```

---

## Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| portfolio.db lock contention | Low | Read-only URI connection |
| portfolio.db path changes | Medium | Config constant in pension_registry.py |
| Existing runs have no pension tags | High | pension_registry_tagger.py one-time script |
| DB schema version mismatch | Low | Only uses standard cols: cagr, maxdd, calmar, sharpe, turnover, tags |
| FSC compliance drift | Low | Tag explicitly; add `fsc_compliant` boolean derived from bond_pct≥30 |

---

## Build Sequence

1. `pension_registry_tagger.py` — tag existing runs (5 min, one-time)
2. `pension_registry.py` — FastAPI router + DB read logic (30 min)
3. `registry.html` — table UI with filters (45 min)
4. `scatter.html` — Plotly bubble chart (30 min)
5. `main.py` — wire routes (10 min)
6. Update `mode3_pension_s35_fsc.py` with db_tags (5 min)

**Total estimated**: ~2 hours

---

## Comparison vs etf-fabless.com/registry

| Feature | etf-fabless.com | Pension Registry |
|---------|----------------|-----------------|
| Strategy table | ✅ SR/MaxDD/Return | ✅ + Tier chip + Bond% |
| Market tab filter | ✅ KRX/USA/etc | ✅ Tier chips (T1-T4) |
| FSC compliance badge | ❌ | ✅ |
| Demand configurator | ❌ | ✅ 나의 전략 찾기 |
| Scatter/bubble chart | ❌ | ✅ MaxDD vs CAGR |
| Equity curve modal | ✅ | ✅ |
| Time window filter | ✅ Full/3M/6M | ✅ |
| Bond% column | ❌ | ✅ |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |

**VERDICT:** NO REVIEWS YET — plan written, ready for review pipeline.
