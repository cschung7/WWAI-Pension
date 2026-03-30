# korea-roboAdvisor-etf-frontier
**URL**: `pension.wwai.app/frontier`
**friends.wwai.app tab**: 퇴직연금 ETF iframe src: `pension.wwai.app` → `pension.wwai.app/frontier`

---

## What We're Building

Standalone efficient frontier page for IRP/DC FSC-admissible ETFs.
Separate from the 4-step wizard — pure portfolio construction lens.

- X-axis: annualised volatility (σ)
- Y-axis: annualised expected return (μ)
- Monte Carlo scatter (10,000 random portfolios, Sharpe heat-coloured)
- Analytical efficient frontier curve (FSC-constrained)
- Individual ETFs as asset-class-coloured dots
- 3 labelled special portfolios: MVP · Max-Sharpe · γ* point

---

## Data

| Item | Source | Coverage |
|------|--------|---------|
| Price history | `AutoML_KrxETF/KRXETFNOTTRAINED/{ticker}.csv` | 877/939 ETFs |
| Universe | `WWAI-Pension/data/irp_eligible_universe.csv` | 939 ETFs, 6 classes |
| UCS signal | `AutoML_KrxETF/Filter2/UCS_LRS/complete_situation_results_*.json` | WWAI 신호 |
| Returns period | 3Y (2023-01-01 → today), **weekly** frequency | 821 ETFs qualify |
| Optimisation subset | Top-100 by UCS score (default), up to 821 | `top_n` param |

**Return computation**:
```python
weekly_close  = daily_close.resample('W-FRI').last()
weekly_ret    = weekly_close.pct_change().dropna()
mu            = weekly_ret.mean() * 52              # annualised
cov           = LedoitWolf().fit(weekly_ret).covariance_ * 52   # shrunk
```

Asset class breakdown of 821 valid ETFs:
- overseas_equity: 314, domestic_theme: 273, bond_money_market: 156
- domestic_equity: 93, mixed_bond: 87, commodity: 16 (→ approx subset for top_n)

---

## Optimisation Engine (`backend/frontier.py`)

### Phase 1 — Individual ETF scatter
Each ETF at its own (σ_i, μ_i). Colour-coded by asset class. Hover shows name + WWAI 신호.

### Phase 2 — Monte Carlo (10,000 draws)
```python
for _ in range(10_000):
    w = dirichlet(ones(n))
    # FSC clip: cap equity classes ≤ 0.70 total, safe ≥ 0.30 total
    equity_mask = asset_class.isin(['domestic_equity','overseas_equity','domestic_theme','commodity'])
    eq_w = w[equity_mask].sum()
    if eq_w > MAX_EQUITY:
        w[equity_mask] *= MAX_EQUITY / eq_w
        w[~equity_mask] *= (1 - MAX_EQUITY) / (1 - eq_w)
    w /= w.sum()
    record(sqrt(w @ cov @ w) * sqrt(52), w @ mu, (w@mu - RF) / sqrt(w@cov@w))
```
Downsample to 3,000 points for API transfer. Colour by Sharpe (viridis).

### Phase 3 — Analytical frontier (50 steps, scipy)
```python
for target_mu in linspace(mu_min, mu_max, 50):
    result = minimize(
        fun         = lambda w: w @ cov @ w,
        x0          = equal_weight,
        constraints = [
            {'type': 'eq',  'fun': lambda w: w.sum() - 1},
            {'type': 'eq',  'fun': lambda w: w @ mu_vec - target_mu},
            {'type': 'ineq','fun': lambda w: MAX_EQUITY - equity_weight(w)},  # FSC ≤70%
            {'type': 'ineq','fun': lambda w: safe_weight(w) - MIN_SAFE},       # FSC ≥30%
        ],
        bounds = [(0, 1)] * n,
        method = 'SLSQP',
    )
    frontier_curve.append((sqrt(result.fun), target_mu))
```

### Special portfolios
| Label | Korean | Constraint | Marker |
|-------|--------|-----------|--------|
| MVP | 최소분산 포트폴리오 | min σ², FSC | ◆ white |
| Max Sharpe | 최대샤프 포트폴리오 | max (μ−3.5%)/σ, FSC | ★ gold |
| γ* point | 내 위험성향 포트폴리오 | frontier point where equity_w = min(1/γ*, 0.70) | ● teal |

---

## Backend (`backend/main.py` additions)

### New routes
```
GET  /frontier                     # serves frontier.html
GET  /frontier/data                # JSON data for chart
```

### Query params for `/frontier/data`
| Param | Default | Options |
|-------|---------|---------|
| `lookback` | `3y` | `1y` / `2y` / `3y` |
| `gamma` | `2.5` | 1.0–10.0 (moves γ* marker) |
| `top_n` | `100` | 10–821 |

### Response schema
```json
{
  "scatter_etfs": [
    {"ticker","name","asset_class","mu","sigma","sharpe","ucs_score","signal"}
  ],
  "monte_carlo": [
    {"sigma","mu","sharpe"}          // 3k downsampled
  ],
  "frontier_curve": [
    {"sigma","mu"}                   // 50 points
  ],
  "special": {
    "mvp":        {"sigma","mu","weights":{"069500":0.12,...}},
    "max_sharpe": {"sigma","mu","sharpe","weights":{...}},
    "gamma_star": {"sigma","mu","equity_pct","weights":{...}}
  },
  "meta": {
    "n_etfs":int, "lookback_days":int, "computed_at":str,
    "regime":str, "rf_rate":0.035
  }
}
```

### Cache strategy
- `_frontier_cache: dict | None = None`
- Pre-computed at startup + refreshed if >6h stale
- Separate cache keys per `(lookback, top_n)`
- γ* marker computed on-the-fly from cached frontier_curve (fast, <1ms)

---

## Frontend (`backend/static/frontier.html`)

### Stack
- **Plotly.js** (CDN) — scatter + line overlay, zoom/pan/hover built-in
- Dark theme matching pension.wwai.app (`#0f172a` background)
- Standalone page — no wizard steps, no nav

### Layout
```
┌──────────────────────────────────────────────────────────────────┐
│  📊 korea-roboAdvisor-etf-frontier                               │
│  한국 IRP/DC 연금 FSC-적격 ETF 효율적 투자선                       │
│  [1Y] [2Y] [3Y★]                         γ: 1.0 ──●── 10.0 2.5 │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  연수익률(μ) ↑                                                    │
│  25% │                    ★ Max-Sharpe                           │
│  20% │               ╱─── (efficient frontier)                   │
│  15% │          ◆ MVP     ● γ*                                   │
│  10% │  · · · · (Monte Carlo heat — Sharpe colour)               │
│   5% │                                                           │
│      └──────────────────────────────────── 연변동성(σ) →         │
│          5%      10%      15%      20%                           │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  [전체] [국내주식] [해외주식] [테마] [채권/MMF] [원자재]           │
├──────────────────────────────────────────────────────────────────┤
│  γ* 포트폴리오: 주식 41.7% | 채권 58.3% | 샤프 0.84              │
└──────────────────────────────────────────────────────────────────┘
```

### Interactions
1. **γ* slider** (1.0–10.0): debounced 300ms → `GET /frontier/data?gamma=X` → moves only the γ* marker, updates equity % callout. Frontier curve + Monte Carlo stay cached on client.
2. **Lookback chips** [1Y/2Y/3Y]: triggers full data reload.
3. **Asset class filter chips**: toggle visibility of scatter_etfs dots only.
4. **Hover tooltip** on any dot:
   ```
   ETF명: KODEX 미국S&P바이오(합성)
   연수익률:  +18.4%
   연변동성:  22.1%
   Sharpe:    0.83
   WWAI 신호: 강한매수 ●●●●
   자산군:    해외주식
   ```
5. **Click on γ* marker**: expand panel showing top-5 ETFs by weight in that portfolio.

---

## friends.wwai.app Change

File: `/mnt/nas/WWAI/WWAI-WB-Friends/index.html`
Line 1053 (one char change):

```js
// Before
frontier: { id: 'iframe-frontier', src: 'https://pension.wwai.app', loading: 'load-frontier' },

// After
frontier: { id: 'iframe-frontier', src: 'https://pension.wwai.app/frontier', loading: 'load-frontier' },
```

No service restart needed (static HTML, served directly).

---

## File Plan

```
WWAI-Pension/
├── backend/
│   ├── frontier.py              # NEW — optimisation engine
│   │   ├── load_returns(tickers, lookback, price_dir)
│   │   ├── run_monte_carlo(mu, cov, asset_classes, n=10_000)
│   │   ├── compute_frontier(mu, cov, asset_classes, steps=50)
│   │   ├── special_portfolios(mu, cov, asset_classes, gamma)
│   │   └── build_frontier_response(lookback, top_n, gamma)
│   ├── main.py                  # Add /frontier + /frontier/data routes
│   └── static/
│       └── frontier.html        # NEW — Plotly.js standalone page
└── docs/
    └── frontier_plan.md         # this file
```

---

## Dependencies (all present in ag environment)

| Package | Use |
|---------|-----|
| `numpy` | matrix ops, Dirichlet sampling |
| `pandas` | price loading, resampling |
| `scipy.optimize.minimize` | analytical frontier (SLSQP) |
| `sklearn.covariance.LedoitWolf` | covariance shrinkage |
| `fastapi` | new routes |
| Plotly.js (CDN) | frontend chart, no install |

---

## Build Order

1. `backend/frontier.py` — standalone module, test with `python3 -c "from backend.frontier import build_frontier_response; print(build_frontier_response('3y',100,2.5)['meta'])"`
2. `backend/main.py` — add 2 routes + cache warmup on startup
3. `backend/static/frontier.html` — Plotly chart + controls
4. `systemctl --user restart wwai-pension`
5. `curl http://localhost:8121/frontier` → verify page
6. `curl http://localhost:8121/frontier/data?gamma=2.5` → verify JSON
7. Update `WWAI-WB-Friends/index.html` line 1053
8. Push both repos to GitHub

---

## Performance Budget

| Step | Time (top_n=100) |
|------|-----------------|
| Load returns (100 ETFs × 3Y weekly) | ~0.5s |
| LedoitWolf fit | ~0.1s |
| Monte Carlo 10k | ~0.5s |
| Analytical frontier 50 steps | ~2.0s |
| **Total cold compute** | **~3.1s** |
| **Cached response** | **<50ms** |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |

**VERDICT:** NO REVIEWS YET
