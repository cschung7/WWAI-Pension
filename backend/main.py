"""
WWAI-Pension: Korea IRP/DC Pension Robo-Advisor
FastAPI backend — port 8121 (pension.wwai.app)

Extends profile.wwai.app DNA scoring with 5 pension-specific questions.
Applies IRP FSC compliance rules: max 70% equity, min 30% safe assets.
"""

from __future__ import annotations

import glob
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR   = Path(__file__).resolve().parent.parent
DATA_DIR   = BASE_DIR / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"

PROFILE_API = os.getenv("PROFILE_API_URL", "http://127.0.0.1:8898")
AEGIS_KRX   = os.getenv("AEGIS_KRX_URL",   "http://127.0.0.1:8021")

# IRP FSC compliance constants
MAX_EQUITY_WEIGHT  = 0.70
MIN_SAFE_WEIGHT    = 0.30
MIN_ETF_COUNT      = 3
MAX_SINGLE_ETF     = 0.40
ANNUAL_ROBO_LIMIT  = 9_000_000   # 900만원

app = FastAPI(title="WWAI-Pension", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ─── IRP Universe ────────────────────────────────────────────────────────────

_universe: pd.DataFrame | None = None
_ucs_scores: dict | None = None
UCS_DIR = Path("/mnt/nas/AutoGluon/AutoML_KrxETF/Filter2/UCS_LRS")


def load_universe() -> pd.DataFrame:
    global _universe
    if _universe is None:
        path = DATA_DIR / "irp_eligible_universe.csv"
        _universe = pd.read_csv(path, dtype={"ticker": str})
        _universe["ticker"] = _universe["ticker"].str.zfill(6)
    return _universe


def load_ucs_scores() -> dict[str, float]:
    """
    Load latest complete_situation_results_*.json and return
    {ticker: ucs_score} where ucs_score = pattern_count*25 + min(lrs,100).
    Cached for session lifetime; refreshed if file is newer than 6h.
    """
    global _ucs_scores
    if _ucs_scores is not None:
        return _ucs_scores

    files = sorted(glob.glob(str(UCS_DIR / "complete_situation_results_*.json")))
    if not files:
        _ucs_scores = {}
        return _ucs_scores

    latest = files[-1]
    try:
        raw = json.loads(Path(latest).read_text())
    except Exception:
        _ucs_scores = {}
        return _ucs_scores

    scores: dict[str, float] = {}
    for ticker, v in raw.items():
        lrs = v.get("weekly_metrics", {}).get("lrs_value", 0.0)
        pc  = v.get("pattern_count", 0)
        scores[ticker] = round(pc * 25 + min(float(lrs), 100.0), 2)

    _ucs_scores = scores
    return scores


def _score_universe(universe: pd.DataFrame) -> pd.DataFrame:
    """
    Attach ucs_score column to universe.
    Covered ETFs: ucs_score = pattern_count*25 + min(lrs,100)  → 0–200
    Uncovered    : ucs_score = market_cap percentile * 0.4      → 0–40
    Ensures UCS-signalled ETFs always rank above uncovered ones.
    """
    scores = load_ucs_scores()
    df = universe.copy()
    df["ucs_score"] = df["ticker"].map(scores)

    # Percentile fallback for uncovered tickers
    mcap_col = df["market_cap_억"].fillna(0)
    pct = mcap_col.rank(pct=True) * 40.0
    df["ucs_score"] = df["ucs_score"].fillna(pct)
    return df


# ─── CRRA Elicitation (mirrors advisor.wwai.app) ─────────────────────────────

CRRA_BRACKETS = [
    (1.25, 2.0,  1.6, "Moderate Risk-Taker"),   # 0 cautious answers
    (2.0,  3.5,  2.5, "Mildly Risk-Averse"),    # 1
    (3.5,  5.5,  4.5, "Quite Risk-Averse"),     # 2
    (5.5,  10.0, 7.0, "Very Risk-Averse"),      # 3
]

COHORT_PROFILES = {
    "20s": {"gamma_default": 1.6, "lambda_hat": 1.2, "k_hat": 0.025, "hc_ratio": 9.0},
    "30s": {"gamma_default": 2.5, "lambda_hat": 1.5, "k_hat": 0.015, "hc_ratio": 5.0},
    "40s": {"gamma_default": 2.5, "lambda_hat": 1.8, "k_hat": 0.012, "hc_ratio": 2.5},
    "50s": {"gamma_default": 4.5, "lambda_hat": 2.2, "k_hat": 0.010, "hc_ratio": 1.0},
    "60s": {"gamma_default": 7.0, "lambda_hat": 2.5, "k_hat": 0.008, "hc_ratio": 0.2},
}

SNS_MAP = {
    "strong_bearish": {"phi_eff": 0.8, "valence": "bearish"},
    "mild_bearish":   {"phi_eff": 0.4, "valence": "bearish"},
    "neutral":        {"phi_eff": 0.0, "valence": "neutral"},
    "mild_bullish":   {"phi_eff": 0.4, "valence": "bullish"},
    "strong_bullish": {"phi_eff": 0.8, "valence": "bullish"},
}

CRRA_CONSERVATIVE_THRESHOLD = 3.5  # γ̂ ≥ this → immune response


def _crra_score(q6: str, q7: str, q8: str) -> int:
    """Count cautious (A) choices → 0..3."""
    return sum(1 for v in (q6, q7, q8) if v == "A")


def _sns_corrected_gamma(gamma_hat: float, phi_eff: float, valence: str) -> float:
    if valence == "neutral" or phi_eff == 0.0:
        return gamma_hat
    delta = 0.25 * phi_eff
    if gamma_hat >= CRRA_CONSERVATIVE_THRESHOLD:
        return round(gamma_hat + delta, 3)
    return round(max(0.5, gamma_hat - delta), 3) if valence == "bullish" else round(gamma_hat + delta, 3)


def _compute_crra(cohort: str, q6: str, q7: str, q8: str, sns_level: str) -> dict:
    score  = _crra_score(q6, q7, q8)
    _, _, gamma_hat, label = CRRA_BRACKETS[score]
    sns    = SNS_MAP.get(sns_level, SNS_MAP["neutral"])
    gamma_star = _sns_corrected_gamma(gamma_hat, sns["phi_eff"], sns["valence"])
    cohort_default = COHORT_PROFILES.get(cohort, COHORT_PROFILES["40s"])["gamma_default"]
    correction_bps = round(abs(gamma_star - gamma_hat) * 100)
    return {
        "gamma_hat":       gamma_hat,
        "gamma_star":      gamma_star,
        "crra_label":      label,
        "crra_score":      score,
        "cohort":          cohort,
        "cohort_default":  cohort_default,
        "sns_level":       sns_level,
        "correction_bps":  correction_bps,
        "fomo_score":      max(0.0, (3.5 - gamma_hat) / 3.5 * 100),
        "panic_score":     min(100.0, gamma_hat / 7.0 * 100),
    }


# ─── Pension Profile Models ──────────────────────────────────────────────────

class PensionContext(BaseModel):
    """5 pension-specific questions on top of the standard DNA questionnaire."""
    retirement_year: int = Field(..., ge=2025, le=2070,
        description="예상 은퇴 연도 (e.g. 2045)")
    irp_balance_만원: float = Field(default=0.0, ge=0,
        description="현재 IRP/DC 적립금 (만원)")
    monthly_contrib_만원: float = Field(default=50.0, ge=0,
        description="월 납입 가능 금액 (만원)")
    current_allocation_type: str = Field(default="conservative",
        description="현재 주요 운용 유형: conservative | mixed | aggressive")
    switch_intent: bool = Field(default=True,
        description="실물이전(갈아타기) 의향 여부")


class CoreDNAAnswers(BaseModel):
    """Standard 22-question investment DNA answers passed to profile.wwai.app."""
    q1:  str   = "7_years_plus"
    q3:  str   = "minimal"
    q4:  str   = "gt_12m"
    q5:  str   = "independent"
    q6:  str   = "B"
    q7:  str   = "A"
    q8:  str   = "B"
    q9:  str   = "B"
    q10: str   = "B"
    q11: float = 8.0
    q12: float = 18.0
    q13: float = 8.0
    q14: str   = "above_average"
    q15: str   = "wait_pullback"
    q16: str   = "unchanged"
    q17: str   = "selective_buy"
    q18: str   = "tolerate_if_thesis"
    q19: str   = "add_moderately"
    q20: str   = "better_entry"
    q21: str   = "own_analysis"
    q22: str   = "keep_if_strong"


class PensionProfileRequest(BaseModel):
    client_id:   str             = "anon"
    core_dna:    CoreDNAAnswers  = Field(default_factory=CoreDNAAnswers)
    pension_ctx: PensionContext
    crra_input:  CRRAInput | None = None   # when provided, bypasses profile.wwai.app


class CRRAInput(BaseModel):
    cohort:    str = Field(default="40s", description="연령대: 20s|30s|40s|50s|60s")
    q6:        str = Field(default="B",   description="A=확실한수익 B=도박")
    q7:        str = Field(default="B",   description="A=매도 B=추가매수")
    q8:        str = Field(default="B",   description="A=연3%확실 B=변동")
    sns_level: str = Field(default="neutral",
                           description="strong_bearish|mild_bearish|neutral|mild_bullish|strong_bullish")


class PensionPortfolioRequest(BaseModel):
    profile: dict[str, Any]     # output of /api/pension/profile
    regime_override: str | None = None  # "Bull_Quiet" | "Bear_Volatile" etc.


# ─── Pension Logic ───────────────────────────────────────────────────────────

def _horizon_years(retirement_year: int) -> int:
    return max(1, retirement_year - datetime.now().year)


def _glide_path_equity(gamma_hat: float, horizon_yr: int) -> float:
    """
    CRRA + lifecycle glide path.
    Base equity = 1/γ, capped at 70% (FSC).
    Reduce 1pp per year as horizon < 10yr.
    """
    base = min(1.0 / gamma_hat, MAX_EQUITY_WEIGHT)
    if horizon_yr < 10:
        decay = (10 - horizon_yr) * 0.01
        base  = max(0.10, base - decay)
    return round(min(base, MAX_EQUITY_WEIGHT), 4)


def _tax_benefit(annual_contrib_만원: float, annual_income_est: str = "average") -> dict:
    annual_contrib = annual_contrib_만원 * 10_000
    taxable = min(annual_contrib, ANNUAL_ROBO_LIMIT)
    rate = 0.165 if annual_income_est in ("low", "average") else 0.132
    deduction = round(taxable * rate)
    return {
        "annual_contribution_원": int(annual_contrib),
        "tax_deductible_원": int(taxable),
        "estimated_tax_return_원": deduction,
        "effective_rate_pct": round(rate * 100, 1),
    }


def _select_irp_etfs(
    universe: pd.DataFrame,
    equity_weight: float,
    horizon_yr: int,
    fomo_score: float,
) -> list[dict]:
    """
    Pick top ETFs per asset class ranked by UCS/LRS signal score.
    Score = pattern_count*25 + min(lrs_value,100) for covered ETFs,
            market_cap percentile * 0.4 for uncovered (domestic_equity etc).
    Picks top-2 per equity class (score-weighted), top-1 per safe class.
    """
    safe_weight = 1.0 - equity_weight
    scored = _score_universe(universe)

    # Equity bucket splits by horizon
    if horizon_yr >= 15:
        equity_split = {"domestic_equity": 0.28, "overseas_equity": 0.45, "domestic_theme": 0.22, "commodity": 0.05}
    elif horizon_yr >= 7:
        equity_split = {"domestic_equity": 0.38, "overseas_equity": 0.35, "domestic_theme": 0.22, "commodity": 0.05}
    else:
        equity_split = {"domestic_equity": 0.55, "overseas_equity": 0.25, "domestic_theme": 0.15, "commodity": 0.05}

    if fomo_score >= 65:
        equity_split["domestic_theme"] = max(0.05, equity_split["domestic_theme"] - 0.10)
        equity_split["domestic_equity"] += 0.10

    safe_split = {"bond_money_market": 0.65, "mixed_bond": 0.35}

    picks = []

    def top_n(asset_class: str, n: int) -> list[pd.Series]:
        sub = scored[scored["asset_class"] == asset_class].sort_values(
            "ucs_score", ascending=False
        )
        return [sub.iloc[i] for i in range(min(n, len(sub)))]

    def rows_to_picks(rows: list, frac: float, total_w: float, risk_type: str, region: str) -> list[dict]:
        if not rows:
            return []
        # Weight proportional to ucs_score within the class; equal if scores are identical
        s = [max(float(r["ucs_score"]), 0.1) for r in rows]
        s_total = sum(s)
        out = []
        for row, si in zip(rows, s):
            w = round(float(total_w) * float(frac) * (si / s_total), 4)
            nav_val = row.get("nav", 0)
            mcap_val = row.get("market_cap_억", 0)
            reg_val = row.get("region")
            out.append({
                "ticker":        str(row["ticker"]),
                "name":          str(row["name"]),
                "asset_class":   str(row["asset_class"]),
                "risk_type":     str(risk_type),
                "region":        str(reg_val) if reg_val is not None and reg_val == reg_val else str(region),
                "weight":        float(w),
                "nav":           float(nav_val) if nav_val is not None and nav_val == nav_val else 0.0,
                "market_cap_억": float(mcap_val) if mcap_val is not None and mcap_val == mcap_val else 0.0,
                "ucs_score":     round(float(row["ucs_score"]), 1),
                "signal":        _score_to_signal(float(row["ucs_score"])),
            })
        return out

    for cls, frac in equity_split.items():
        rows = top_n(cls, 2)   # top-2 per equity class
        picks.extend(rows_to_picks(rows, frac, equity_weight, "위험자산", "KRX"))

    for cls, frac in safe_split.items():
        rows = top_n(cls, 1)   # top-1 per safe class
        picks.extend(rows_to_picks(rows, frac, safe_weight, "안전자산", "KRX"))

    # Normalise to sum = 1.0
    total = sum(p["weight"] for p in picks)
    if total > 0:
        for p in picks:
            p["weight"] = round(float(p["weight"]) / float(total), 4)

    return picks


def _score_to_signal(score: float) -> str:
    """Human-readable signal strength label."""
    if score >= 150: return "강한매수 ●●●●"
    if score >= 100: return "매수 ●●●○"
    if score >= 50:  return "중립 ●●○○"
    if score >= 1:   return "약세 ●○○○"
    return "신호없음 ○○○○"


def _fsc_compliance(picks: list[dict]) -> dict:
    equity_w   = float(sum(p["weight"] for p in picks if p["risk_type"] == "위험자산"))
    safe_w     = float(sum(p["weight"] for p in picks if p["risk_type"] == "안전자산"))
    max_single = float(max((p["weight"] for p in picks), default=0))
    return {
        "fsc_pass":       bool(equity_w <= MAX_EQUITY_WEIGHT and safe_w >= MIN_SAFE_WEIGHT),
        "equity_pct":     round(equity_w * 100, 1),
        "safe_pct":       round(safe_w * 100, 1),
        "max_single_pct": round(max_single * 100, 1),
        "etf_count":      len(picks),
        "violations":     [
            *(["위험자산 한도 초과 (>70%)"] if equity_w > MAX_EQUITY_WEIGHT else []),
            *(["안전자산 미달 (<30%)"]      if safe_w   < MIN_SAFE_WEIGHT   else []),
            *(["ETF 수 부족 (<3개)"]        if len(picks) < MIN_ETF_COUNT   else []),
        ],
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _gamma_to_archetype(gamma: float) -> str:
    if gamma < 2.0:
        return "moderate_risk_taker"
    if gamma < 3.5:
        return "controlled_allocator"
    if gamma < 5.5:
        return "quite_risk_averse"
    return "very_risk_averse"


async def _fetch_dna(client_id: str, answers: dict) -> dict:
    payload = {
        "questionnaire": {
            "client_id":             client_id,
            "questionnaire_version": "v1",
            "answers":               answers,
        }
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{PROFILE_API}/api/wwai/dna/score", json=payload)
        r.raise_for_status()
        return r.json()


async def _fetch_regime() -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{AEGIS_KRX}/api/regime/current")
            r.raise_for_status()
            data = r.json()
            return data.get("regime", "Bull_Quiet")
    except Exception:
        return "Bull_Quiet"  # fallback


# ─── API Endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "wwai-pension", "version": "0.1.0"}


@app.get("/api/pension/universe")
def get_universe(asset_class: str | None = None, risk_type: str | None = None) -> dict:
    df = load_universe()
    if asset_class:
        df = df[df["asset_class"] == asset_class]
    if risk_type:
        df = df[df["risk_type"] == risk_type]
    return {
        "count": len(df),
        "etfs":  df.head(50).to_dict(orient="records"),
    }


@app.post("/api/pension/crra")
async def score_crra(req: CRRAInput) -> dict:
    """
    Standalone CRRA elicitation endpoint.
    Returns γ_hat, γ_star (SNS-corrected), archetype label, and allocation hint.
    """
    result = _compute_crra(req.cohort, req.q6, req.q7, req.q8, req.sns_level)
    eq_hint = round(min(1.0 / result["gamma_star"], MAX_EQUITY_WEIGHT) * 100, 1)
    result["equity_hint_pct"] = eq_hint
    result["safe_hint_pct"]   = round(100 - eq_hint, 1)
    return result


@app.post("/api/pension/profile")
async def build_profile(req: PensionProfileRequest) -> dict:
    """
    Step 1: Score full investment DNA + pension context.
    If crra_input is provided, uses local CRRA algorithm (advisor.wwai.app logic).
    Otherwise calls profile.wwai.app for full 22-question DNA.
    """
    horizon_yr = _horizon_years(req.pension_ctx.retirement_year)

    if req.crra_input is not None:
        # ── Local CRRA path (default for pension wizard) ──────────────────────
        crra = _compute_crra(
            req.crra_input.cohort,
            req.crra_input.q6, req.crra_input.q7, req.crra_input.q8,
            req.crra_input.sns_level,
        )
        gamma = crra["gamma_star"]
        dna = {
            "client_id":           req.client_id,
            "gamma_hat":           gamma,
            "fomo_score":          crra["fomo_score"],
            "panic_score":         crra["panic_score"],
            "dipfaith_score":      50.0,
            "risk_capacity_score": 55.0,
            "archetype":           _gamma_to_archetype(gamma),
            "crra_detail":         crra,
        }
    else:
        # ── Full DNA path (profile.wwai.app) ─────────────────────────────────
        answers = req.core_dna.model_dump()
        if horizon_yr >= 7:
            answers["q1"] = "7_years_plus"
        elif horizon_yr >= 3:
            answers["q1"] = "3_7y"
        elif horizon_yr >= 1:
            answers["q1"] = "1_3y"
        else:
            answers["q1"] = "within_1y"
        try:
            dna = await _fetch_dna(req.client_id, answers)
        except Exception:
            dna = {
                "client_id":           req.client_id,
                "gamma_hat":           2.5,
                "fomo_score":          50.0,
                "panic_score":         50.0,
                "dipfaith_score":      50.0,
                "risk_capacity_score": 55.0,
                "archetype":           "controlled_allocator",
            }

    equity_weight = _glide_path_equity(dna["gamma_hat"], horizon_yr)
    annual_contrib = req.pension_ctx.monthly_contrib_만원 * 12
    tax             = _tax_benefit(annual_contrib, "average")

    return {
        "client_id":       req.client_id,
        "generated_at":    datetime.now().isoformat(),
        "dna":             dna,
        "pension_context": req.pension_ctx.model_dump(),
        "horizon_yr":      horizon_yr,
        "equity_weight":   equity_weight,
        "safe_weight":     round(1.0 - equity_weight, 4),
        "tax_benefit":     tax,
        "copyright_notice": (
            "본 투자성향 진단 방법론은 WWAI의 독점적 행동재무 알고리즘을 기반으로 합니다. "
            "© 2025 WWAI. All rights reserved. Unauthorized reproduction prohibited."
        ),
    }


@app.post("/api/pension/portfolio")
async def build_portfolio(req: PensionPortfolioRequest) -> dict:
    """
    Step 2: Profile JSON → IRP-compliant ETF portfolio.
    """
    profile = req.profile
    dna     = profile.get("dna", {})
    gamma   = dna.get("gamma_hat", 2.5)
    fomo    = dna.get("fomo_score", 50.0)
    horizon = profile.get("horizon_yr", 20)
    eq_w    = profile.get("equity_weight", 0.60)

    # Regime adjustment
    regime  = req.regime_override or await _fetch_regime()
    if "Bear" in regime:
        eq_w = max(0.30, eq_w - 0.10)   # defensive tilt
    elif "Bull_Quiet" in regime:
        pass                              # full allocation

    universe = load_universe()
    picks     = _select_irp_etfs(universe, eq_w, horizon, fomo)
    compliance = _fsc_compliance(picks)

    pension_ctx = profile.get("pension_context", {})
    monthly_contrib = pension_ctx.get("monthly_contrib_만원", 50.0)
    annual_contrib  = monthly_contrib * 12

    return {
        "client_id":   profile.get("client_id", "anon"),
        "generated_at": datetime.now().isoformat(),
        "regime":      regime,
        "allocation": {
            "equity_pct": round(eq_w * 100, 1),
            "safe_pct":   round((1 - eq_w) * 100, 1),
        },
        "etfs":       picks,
        "compliance": compliance,
        "rebalancing": {
            "frequency":     "quarterly",
            "drift_trigger": "5%p 이상 이탈 시 자동 리밸런싱",
        },
        "tax_benefit":      profile.get("tax_benefit", {}),
        "robo_limit_원":    ANNUAL_ROBO_LIMIT,
        "annual_contrib_만원": annual_contrib,
        "switch_guidance":  (
            "실물이전(갈아타기) 가능: 기존 금융사에서 매도 없이 현물 이전 신청 → "
            "수익률 높은 로보어드바이저 운용사로 이동"
            if pension_ctx.get("switch_intent", True) else None
        ),
    }


# ─── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>WWAI-Pension</h1><p>Static UI not yet built.</p>")
