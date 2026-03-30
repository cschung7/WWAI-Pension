# WWAI-Pension: Korea Pension Robo-Advisor — Implementation Plan

**Date**: 2026-03-30
**Status**: Planning
**Branch**: master

---

## Context

The Korean FSC opened IRP robo-advisor discretionary management (혁신금융서비스) on 2025-03-27.
Fount + Hana Bank launched first. Competitors (Mirae Asset, Korea Investment Trust, Quarterback,
December&Company) go live April 2025.

WWAI's edge: Fiedler-gated theme cohesion + cross-market regime signals (AEGIS) vs generic TDF/multi-asset.

**Existing WWAI assets we can reuse:**
- `profile.wwai.app` (port 8898) — 22-question DNA profiler → γ*, FOMO/PANIC/DIPFAITH, ETF fit
- `advisor.wwai.app` (port 8118) — 3-step CRRA binary lottery + SNS correction (cohort 20s–60s)
- `etf-fabless.wwai.app/frontier` — ETF frontier dashboard (KRX-focused)
- `krx-fsc-compliance-validator` agent — validates portfolios against FSC rules
- `WWAI-AEGIS` — regime-gated ETF strategies for KRX/USA/Japan/India/HK/China

---

## Architecture

```
[User] → pension.wwai.app
    ↓
[Step 1] Profile Questionnaire (extend profile.wwai.app)
    - Retirement date / horizon
    - Monthly contribution capacity
    - Existing IRP balance
    - CRRA elicitation (3 questions from advisor.wwai.app)
    - SNS sensitivity (FOMO/PANIC from profile.wwai.app)
    ↓
[Step 2] IRP Universe Filter
    - Pull KRX pension-eligible ETF list (FSC 퇴직연금 ETF list)
    - Add international ETFs (USA/Japan/India/HK/China) where DC allows
    - Filter by IRP rules: max 70% equity, no leverage, no inverse
    ↓
[Step 3] Portfolio Construction Engine
    - Risk profile (γ*) → equity/bond target allocation
    - AEGIS regime gate → which themes/markets are BUY
    - Fiedler cohesion → top-3 ETFs per asset class
    - FSC compliance check: max 70% equity, diversification
    ↓
[Step 4] Pension Frontier Output (pension.wwai.app)
    - Personalized ETF grid (like etf-fabless/frontier but IRP-filtered)
    - Show: allocation %, expected return, max drawdown, IRP tax benefit
    - Rebalancing trigger (quarterly or regime change)
    - One-click "갈아타기 (실물이전)" guidance
```

---

## Todo List

### Phase 0: Research & Regulation (2 days human / ~1 hour CC)

- [ ] **0.1** Map IRP eligible ETF rules
  - FSC 퇴직연금 감독규정: max 70% 위험자산 (주식형 ETF)
  - No leverage/inverse ETFs in IRP
  - Overseas ETF limits in IRP vs DC
  - 디폴트옵션 rules (TDF vs balanced fund as default)
  - Koscom testbed algorithm submission requirements

- [ ] **0.2** List IRP-eligible ETFs from KRX
  - Scrape/download FSC approved ETF list
  - Filter from existing KRXETF universe
  - Flag which are pension-eligible (연금계좌 ETF 목록)

- [ ] **0.3** Document patent/IP protection strategy
  - Questionnaire → "proprietary behavioral scoring methodology" copyright notice
  - Algorithm → trade secret (not filed patent for speed)
  - Unique elements: SNS-corrected CRRA + Fiedler-gated ETF selection for IRP

---

### Phase 1: Extended Profile Questionnaire (3 days human / ~1.5 hours CC)

- [ ] **1.1** Extend `profile.wwai.app` DNA profiler with pension context
  - Add 5 IRP-specific questions:
    1. 은퇴 예정 연도 (retirement year) → investment horizon
    2. 현재 IRP/DC 적립금 (current balance)
    3. 월 납입 가능 금액 (monthly contribution capacity)
    4. 현재 자산 유형 (mostly 원리금보장 / 실적배당?)
    5. 갈아타기 의향 (willing to switch providers?)
  - Map horizon → lifecycle equity glide path target
  - Output: full profile JSON including γ*, FOMO/PANIC, horizon, capacity

- [ ] **1.2** IP protection markup
  - Add copyright notice in questionnaire HTML
  - Footer: "본 투자성향 진단 방법론은 WWAI의 독점적 행동재무 알고리즘을 기반으로 합니다."
  - Timestamp + user session logged to DB for evidence trail

---

### Phase 2: IRP ETF Universe Builder (2 days human / ~1 hour CC)

- [ ] **2.1** Build pension-eligible ETF universe
  - Source: KRX ETF list filtered by 연금계좌 가능 여부
  - Base path: `/mnt/nas/AutoGluon/AutoML_KrxETF/DB/db_naver.csv`
  - Add tags: `irp_eligible`, `asset_class` (equity/bond/mixed/overseas), `region`
  - Include overseas ETFs (TIGER 미국나스닥100, KODEX 선진국MSCI, etc.)

- [ ] **2.2** Cross-market extension
  - Map KRX pension ETFs → WWAI-AEGIS themes (USA/Japan/India/HK/China)
  - For DC accounts: add up to 30% international allocation
  - Source signals from existing AEGIS regime engines (ports 8000–8006)

- [ ] **2.3** FSC compliance rules engine
  - `max_equity_weight = 0.70` for IRP
  - No leverage (배율 > 1x), no inverse (-1x, -2x)
  - Min diversification: ≥ 3 ETFs
  - Rebalancing frequency: quarterly minimum
  - Extend existing `krx-fsc-compliance-validator` agent

---

### Phase 3: Portfolio Construction Engine (3 days human / ~2 hours CC)

- [ ] **3.1** Risk profile → target allocation
  - γ* from profile → equity % via CRRA utility maximization
  - Cohort glide path overlay (reduces equity as horizon shrinks)
  - Formula: `equity_pct = min(0.70, max(0.10, 1/γ* × horizon_factor))`

- [ ] **3.2** AEGIS regime gate integration
  - Pull current regime for KRX + selected markets from AEGIS backends
  - Bull Quiet → full equity allocation; Bear Volatile → defensive tilt
  - Use Fiedler λ₂ cohesion to rank ETFs within each asset class

- [ ] **3.3** Final portfolio output schema
  ```json
  {
    "profile": { "gamma": 2.5, "horizon_yr": 20, "fomo": 0.3 },
    "allocation": { "equity": 0.65, "bond": 0.25, "overseas": 0.10 },
    "etfs": [
      { "ticker": "069500", "name": "KODEX 200", "weight": 0.35, "irp_eligible": true },
      { "ticker": "102110", "name": "TIGER 200", "weight": 0.30, "irp_eligible": true },
      { "ticker": "195930", "name": "KODEX 선진국MSCI", "weight": 0.10, "irp_eligible": true },
      { "ticker": "148070", "name": "KOSEF 국고채10년", "weight": 0.25, "irp_eligible": true }
    ],
    "compliance": { "fsc_pass": true, "equity_pct": 0.65, "max_dd_est": -0.18 },
    "rebalance_trigger": "quarterly or regime change",
    "tax_benefit": { "annual_contribution": 9000000, "tax_deduction": 1485000 }
  }
  ```

---

### Phase 4: Pension Frontier App — pension.wwai.app (4 days human / ~3 hours CC)

- [ ] **4.1** FastAPI backend (`/mnt/nas/WWAI/WWAI-Pension/backend/main.py`)
  - `POST /api/pension/profile` → save questionnaire → returns profile JSON
  - `POST /api/pension/portfolio` → profile JSON → returns portfolio + compliance
  - `GET /api/pension/universe` → full IRP-eligible ETF list
  - `GET /api/pension/regime` → current regime snapshot for pension context

- [ ] **4.2** Frontend (`static/index.html`)
  - 4-step wizard UI (dark theme, WWAI design system)
    1. Profile questionnaire (embedded from profile.wwai.app)
    2. Allocation preview (equity/bond/overseas donut)
    3. ETF grid (like etf-fabless frontier, IRP-filtered)
    4. Tax benefit summary + 갈아타기 guide
  - Mobile-first (most IRP users check on phone)
  - Copyright/IP notice in footer

- [ ] **4.3** Service deployment
  - Port: `8120` (reuse slot from WWAI-FnGuide-Like or new)
  - Actually use port `8121` (new)
  - Cloudflare: `pension.wwai.app → 127.0.0.1:8121`
  - Systemd service: `wwai-pension`
  - Install script: `/mnt/nas/WWAI/scripts/install-pension.sh`

- [ ] **4.4** Register in service-registry.md + MEMORY.md

---

### Phase 5: Koscom Testbed Prep (stretch goal)

- [ ] **5.1** Document algorithm for Koscom testbed submission
  - Algorithm description: CRRA elicitation + SNS correction + Fiedler-gated ETF selection
  - Backtested performance (use AEGIS KRX backtest results)
  - Risk controls: max drawdown limits, rebalancing rules

- [ ] **5.2** Paper trading track record
  - Start paper IRP portfolio at launch
  - Daily P&L log (similar to DAWN/GATE incubators)
  - 6-month track record → Koscom submission

---

## Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Profile backend | Extend profile.wwai.app (port 8898) | Already has γ*, FOMO/PANIC — don't duplicate |
| ETF universe | KRX pension-eligible + select international | IRP rules allow international ETFs |
| Max equity | 70% hard cap | FSC IRP regulation |
| Regime signal | AEGIS KRX (port 8021) primary | Best Fiedler signal for Korean market |
| Portfolio style | HRP-like with regime overlay | Reduces drawdown vs MCW |
| App port | 8121 | Next available in registry |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |

**VERDICT:** NO REVIEWS YET
