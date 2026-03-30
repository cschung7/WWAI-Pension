"""
korea-roboAdvisor-etf-frontier
Efficient frontier computation for IRP/DC FSC-admissible ETFs.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from math import sqrt
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.covariance import LedoitWolf

log = logging.getLogger("frontier")

# ── Paths ──────────────────────────────────────────────────────────────────
PRICE_DIR   = Path("/mnt/nas/AutoGluon/AutoML_KrxETF/KRXETFNOTTRAINED")
UNIVERSE_CSV = Path(__file__).parent.parent / "data" / "irp_eligible_universe.csv"
UCS_DIR      = Path("/mnt/nas/AutoGluon/AutoML_KrxETF/Filter2/UCS_LRS")

# ── FSC constants ──────────────────────────────────────────────────────────
MAX_EQUITY = 0.70
MIN_SAFE   = 0.30
RF_RATE    = 0.035   # 3.5% risk-free (KRX money market proxy)

EQUITY_CLASSES = {"domestic_equity", "overseas_equity", "domestic_theme", "commodity"}
SAFE_CLASSES   = {"bond_money_market", "mixed_bond"}

# ── In-memory cache ────────────────────────────────────────────────────────
_cache: dict[str, Any] = {}   # key: (lookback, top_n) → {data, ts}
CACHE_TTL_SECONDS = 6 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_universe() -> pd.DataFrame:
    return pd.read_csv(UNIVERSE_CSV, encoding="utf-8-sig", dtype={"ticker": str})


def _load_ucs_scores() -> dict[str, float]:
    files = sorted(UCS_DIR.glob("complete_situation_results_*.json"))
    if not files:
        return {}
    with open(files[-1], encoding="utf-8") as f:
        raw = json.load(f)
    scores: dict[str, float] = {}
    for ticker, info in raw.items():
        try:
            pc  = info.get("pattern_count", 0) or 0
            lrs = info.get("overall_assessment", {}).get("lrs_value", 0) or 0
            scores[str(ticker)] = float(pc) * 25 + min(float(lrs), 100.0)
        except Exception:
            pass
    return scores


def load_returns(tickers: list[str], lookback: str = "3y") -> pd.DataFrame:
    """
    Load weekly close returns for each ticker.
    Returns DataFrame shape (weeks, n_tickers), columns = tickers.
    """
    days = {"1y": 365, "2y": 730, "3y": 1095}.get(lookback, 1095)
    cutoff = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    frames = {}
    for ticker in tickers:
        path = PRICE_DIR / f"{ticker}.csv"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path, usecols=["Date", "close"])
            df = df[df["Date"] >= cutoff].copy()
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.set_index("Date").sort_index()
            weekly = df["close"].resample("W-FRI").last().dropna()
            if len(weekly) >= 40:
                frames[ticker] = weekly
        except Exception:
            pass

    if not frames:
        return pd.DataFrame()

    prices = pd.DataFrame(frames)

    # Find latest common start date (intersection of all series)
    # Keep only ETFs that start within 30 weeks of the latest start
    start_dates = prices.apply(lambda col: col.first_valid_index())
    latest_start = start_dates.max()
    # Drop ETFs whose data starts more than 30 weeks later than the median start
    median_start = start_dates.sort_values().iloc[len(start_dates) // 2]
    cutoff_start = median_start + pd.Timedelta(weeks=30)
    keep = start_dates[start_dates <= cutoff_start].index
    prices = prices[keep]

    # Forward-fill up to 3 weeks (handles holidays/trading halts), then restrict
    # to the period where ALL kept ETFs have data
    prices = prices.ffill(limit=3)
    prices = prices.dropna()          # rows where all have data
    prices = prices.dropna(axis=1)    # any col still with NaN → drop

    if prices.shape[1] < 3 or len(prices) < 40:
        return pd.DataFrame()

    returns = prices.pct_change().dropna()
    return returns


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio math helpers
# ─────────────────────────────────────────────────────────────────────────────

def _equity_frac(w: np.ndarray, is_equity: np.ndarray) -> float:
    return float(w[is_equity].sum())


def _safe_frac(w: np.ndarray, is_safe: np.ndarray) -> float:
    return float(w[is_safe].sum())


def _fsc_clip(w: np.ndarray, is_equity: np.ndarray, is_safe: np.ndarray) -> np.ndarray:
    """Hard-clip weights to satisfy FSC 70/30 constraints, then renormalise."""
    w = w.copy()
    eq = w[is_equity].sum()
    if eq > MAX_EQUITY and eq > 0:
        w[is_equity] *= MAX_EQUITY / eq
    sf = w[is_safe].sum()
    sf_needed = 1.0 - w[is_equity].sum()
    if sf < MIN_SAFE * w.sum() and sf_needed > 0:
        w[is_safe] = w[is_safe] / sf * sf_needed if sf > 0 else np.ones(is_safe.sum()) * sf_needed / is_safe.sum()
    total = w.sum()
    if total > 0:
        w /= total
    return w


def _port_stats(w: np.ndarray, mu: np.ndarray, cov: np.ndarray, weeks: int) -> tuple[float, float, float]:
    ann_mu  = float(w @ mu) * weeks
    ann_var = float(w @ cov @ w) * weeks
    ann_sig = sqrt(max(ann_var, 1e-10))
    sharpe  = (ann_mu - RF_RATE) / ann_sig
    return ann_sig, ann_mu, sharpe


# ─────────────────────────────────────────────────────────────────────────────
# Monte Carlo
# ─────────────────────────────────────────────────────────────────────────────

def run_monte_carlo(
    mu: np.ndarray,
    cov: np.ndarray,
    is_equity: np.ndarray,
    is_safe: np.ndarray,
    weeks: int,
    n: int = 10_000,
) -> list[dict]:
    rng = np.random.default_rng(42)
    results = []
    n_assets = len(mu)
    for _ in range(n):
        raw = rng.dirichlet(np.ones(n_assets))
        w = _fsc_clip(raw, is_equity, is_safe)
        sig, ret, sharpe = _port_stats(w, mu, cov, weeks)
        results.append({"sigma": round(sig, 4), "mu": round(ret, 4), "sharpe": round(sharpe, 3)})

    # Downsample to 3,000 for transfer — keep full Sharpe range
    if len(results) > 3000:
        results.sort(key=lambda x: x["sharpe"])
        step = len(results) // 3000
        results = results[::step][:3000]
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Analytical frontier
# ─────────────────────────────────────────────────────────────────────────────

def compute_frontier(
    mu: np.ndarray,
    cov: np.ndarray,
    is_equity: np.ndarray,
    is_safe: np.ndarray,
    weeks: int,
    steps: int = 50,
) -> list[dict]:
    n = len(mu)
    ann_mu = mu * weeks
    ann_cov = cov * weeks

    eq_constraint = {"type": "ineq", "fun": lambda w: MAX_EQUITY - w[is_equity].sum()}
    safe_constraint = {"type": "ineq", "fun": lambda w: w[is_safe].sum() - MIN_SAFE}
    sum_constraint = {"type": "eq",  "fun": lambda w: w.sum() - 1.0}
    bounds = [(0.0, 1.0)] * n

    # Find feasible mu range first
    w0 = np.ones(n) / n
    # Min-return portfolio (MVP)
    res_min = minimize(
        lambda w: w @ ann_cov @ w,
        w0, method="SLSQP",
        bounds=bounds,
        constraints=[sum_constraint, eq_constraint, safe_constraint],
        options={"ftol": 1e-9, "maxiter": 500},
    )
    if not res_min.success:
        return []

    mu_min = float(res_min.x @ ann_mu)

    # Max-return feasible
    res_max = minimize(
        lambda w: -w @ ann_mu,
        w0, method="SLSQP",
        bounds=bounds,
        constraints=[sum_constraint, eq_constraint, safe_constraint],
        options={"ftol": 1e-9, "maxiter": 500},
    )
    mu_max = float(res_max.x @ ann_mu) if res_max.success else float(ann_mu.max())

    curve = []
    for target in np.linspace(mu_min, mu_max, steps):
        ret_constraint = {"type": "eq", "fun": lambda w, t=target: w @ ann_mu - t}
        result = minimize(
            lambda w: w @ ann_cov @ w,
            w0, method="SLSQP",
            bounds=bounds,
            constraints=[sum_constraint, ret_constraint, eq_constraint, safe_constraint],
            options={"ftol": 1e-9, "maxiter": 500},
        )
        if result.success:
            sig = sqrt(max(float(result.fun), 1e-10))
            curve.append({"sigma": round(sig, 4), "mu": round(target, 4)})
        w0 = result.x if result.success else np.ones(n) / n  # warm-start

    return curve


# ─────────────────────────────────────────────────────────────────────────────
# Special portfolios
# ─────────────────────────────────────────────────────────────────────────────

def special_portfolios(
    mu: np.ndarray,
    cov: np.ndarray,
    tickers: list[str],
    is_equity: np.ndarray,
    is_safe: np.ndarray,
    weeks: int,
    gamma: float = 2.5,
) -> dict:
    n = len(mu)
    ann_mu = mu * weeks
    ann_cov = cov * weeks
    w0 = np.ones(n) / n
    bounds = [(0.0, 1.0)] * n
    base_cons = [
        {"type": "eq",  "fun": lambda w: w.sum() - 1.0},
        {"type": "ineq","fun": lambda w: MAX_EQUITY - w[is_equity].sum()},
        {"type": "ineq","fun": lambda w: w[is_safe].sum() - MIN_SAFE},
    ]

    def _weights_dict(w: np.ndarray, threshold: float = 0.005) -> dict:
        return {tickers[i]: round(float(w[i]), 4) for i in range(n) if w[i] > threshold}

    # MVP
    res_mvp = minimize(lambda w: w @ ann_cov @ w, w0, method="SLSQP",
                       bounds=bounds, constraints=base_cons,
                       options={"ftol": 1e-9, "maxiter": 500})
    mvp_w = res_mvp.x if res_mvp.success else w0
    mvp_sig, mvp_mu, mvp_sr = _port_stats(mvp_w, mu, cov, weeks)

    # Max Sharpe
    res_ms = minimize(
        lambda w: -(w @ ann_mu - RF_RATE) / sqrt(max(w @ ann_cov @ w, 1e-10)),
        w0, method="SLSQP",
        bounds=bounds, constraints=base_cons,
        options={"ftol": 1e-9, "maxiter": 500},
    )
    ms_w = res_ms.x if res_ms.success else w0
    ms_sig, ms_mu, ms_sr = _port_stats(ms_w, mu, cov, weeks)

    # γ* portfolio: target equity weight = min(1/γ, 0.70)
    target_eq = min(1.0 / max(gamma, 0.1), MAX_EQUITY)
    gstar_cons = base_cons + [
        {"type": "eq", "fun": lambda w: w[is_equity].sum() - target_eq},
    ]
    res_gstar = minimize(
        lambda w: -(w @ ann_mu - RF_RATE) / sqrt(max(w @ ann_cov @ w, 1e-10)),
        w0, method="SLSQP",
        bounds=bounds, constraints=gstar_cons,
        options={"ftol": 1e-9, "maxiter": 500},
    )
    gs_w = res_gstar.x if res_gstar.success else w0
    gs_sig, gs_mu, gs_sr = _port_stats(gs_w, mu, cov, weeks)

    return {
        "mvp": {
            "sigma": round(mvp_sig, 4), "mu": round(mvp_mu, 4),
            "sharpe": round(mvp_sr, 3), "weights": _weights_dict(mvp_w),
        },
        "max_sharpe": {
            "sigma": round(ms_sig, 4), "mu": round(ms_mu, 4),
            "sharpe": round(ms_sr, 3), "weights": _weights_dict(ms_w),
        },
        "gamma_star": {
            "sigma": round(gs_sig, 4), "mu": round(gs_mu, 4),
            "sharpe": round(gs_sr, 3),
            "equity_pct": round(float(gs_w[is_equity].sum()) * 100, 1),
            "safe_pct":   round(float(gs_w[is_safe].sum()) * 100, 1),
            "weights": _weights_dict(gs_w),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main builder
# ─────────────────────────────────────────────────────────────────────────────

def build_frontier_response(lookback: str = "3y", top_n: int = 100, gamma: float = 2.5) -> dict:
    cache_key = f"{lookback}_{top_n}"
    now = time.time()

    # γ* is cheap — compute on top of cached base if available
    base = _cache.get(cache_key)
    if base and (now - base["ts"]) < CACHE_TTL_SECONDS:
        # Recompute only gamma_star with new gamma
        tickers = base["tickers"]
        mu_arr  = base["mu_arr"]
        cov_arr = base["cov_arr"]
        is_eq   = base["is_equity"]
        is_safe = base["is_safe"]
        weeks   = base["weeks"]
        specials = special_portfolios(mu_arr, cov_arr, tickers, is_eq, is_safe, weeks, gamma)
        resp = dict(base["response"])
        resp["special"] = specials
        resp["meta"]["gamma"] = gamma
        return resp

    t0 = time.time()
    log.info(f"Computing frontier: lookback={lookback} top_n={top_n}")

    universe = _load_universe()
    ucs      = _load_ucs_scores()

    # Score and rank universe
    universe["ucs_score"] = universe["ticker"].map(ucs).fillna(0.0)
    # Use top_n by ucs_score among those with price data
    universe = universe[universe["ticker"].apply(
        lambda t: (PRICE_DIR / f"{t}.csv").exists()
    )].copy()
    universe = universe.nlargest(top_n, "ucs_score").reset_index(drop=True)

    tickers = universe["ticker"].tolist()
    asset_classes = universe.set_index("ticker")["asset_class"].to_dict()

    # Load returns
    ret_df = load_returns(tickers, lookback)
    if ret_df.empty or len(ret_df.columns) < 5:
        raise ValueError("Insufficient return data")

    valid_tickers = ret_df.columns.tolist()
    n = len(valid_tickers)
    weeks_per_year = 52
    weeks_actual = len(ret_df)

    mu_arr  = ret_df.mean().values          # weekly mean
    lw = LedoitWolf()
    lw.fit(ret_df.values)
    cov_arr = lw.covariance_                # weekly covariance

    ac = [asset_classes.get(t, "domestic_equity") for t in valid_tickers]
    is_equity = np.array([c in EQUITY_CLASSES for c in ac])
    is_safe   = np.array([c in SAFE_CLASSES   for c in ac])

    log.info(f"  {n} ETFs, {weeks_actual} weekly obs — running Monte Carlo…")
    mc = run_monte_carlo(mu_arr, cov_arr, is_equity, is_safe, weeks_per_year)

    log.info("  Monte Carlo done — computing analytical frontier…")
    curve = compute_frontier(mu_arr, cov_arr, is_equity, is_safe, weeks_per_year)

    log.info("  Frontier done — computing special portfolios…")
    specials = special_portfolios(mu_arr, cov_arr, valid_tickers, is_equity, is_safe, weeks_per_year, gamma)

    # Individual ETF scatter
    ann_mu  = mu_arr * weeks_per_year
    ann_sig = ret_df.std().values * sqrt(weeks_per_year)
    ucs_map = ucs

    def _score_to_signal(score: float) -> str:
        if score >= 150: return "강한매수 ●●●●"
        if score >= 100: return "매수 ●●●○"
        if score >= 50:  return "중립 ●●○○"
        if score >= 1:   return "약세 ●○○○"
        return "신호없음 ○○○○"

    scatter_etfs = []
    name_map = universe.set_index("ticker")["name"].to_dict()
    for i, t in enumerate(valid_tickers):
        sc = float(ucs_map.get(t, 0))
        scatter_etfs.append({
            "ticker":      t,
            "name":        str(name_map.get(t, t)),
            "asset_class": ac[i],
            "mu":          round(float(ann_mu[i]), 4),
            "sigma":       round(float(ann_sig[i]), 4),
            "sharpe":      round((float(ann_mu[i]) - RF_RATE) / max(float(ann_sig[i]), 0.001), 3),
            "ucs_score":   round(sc, 1),
            "signal":      _score_to_signal(sc),
        })

    elapsed = round(time.time() - t0, 2)
    log.info(f"  Done in {elapsed}s")

    response = {
        "scatter_etfs":   scatter_etfs,
        "monte_carlo":    mc,
        "frontier_curve": curve,
        "special":        specials,
        "meta": {
            "n_etfs":       n,
            "lookback":     lookback,
            "lookback_days": {"1y": 365, "2y": 730, "3y": 1095}[lookback],
            "weeks":        weeks_actual,
            "computed_at":  datetime.now().isoformat(),
            "compute_sec":  elapsed,
            "rf_rate":      RF_RATE,
            "gamma":        gamma,
        },
    }

    _cache[cache_key] = {
        "ts": now, "response": response,
        "tickers": valid_tickers,
        "mu_arr": mu_arr, "cov_arr": cov_arr,
        "is_equity": is_equity, "is_safe": is_safe,
        "weeks": weeks_per_year,
    }
    return response


# ── Standalone test ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t0 = time.time()
    resp = build_frontier_response("3y", 60, 2.5)
    m = resp["meta"]
    sp = resp["special"]
    print(f"\n=== korea-roboAdvisor-etf-frontier ===")
    print(f"ETFs: {m['n_etfs']}  weeks: {m['weeks']}  computed in {m['compute_sec']}s")
    print(f"Frontier points: {len(resp['frontier_curve'])}  MC points: {len(resp['monte_carlo'])}")
    print(f"MVP:        σ={sp['mvp']['sigma']:.1%}  μ={sp['mvp']['mu']:.1%}  SR={sp['mvp']['sharpe']:.2f}")
    print(f"Max-Sharpe: σ={sp['max_sharpe']['sigma']:.1%}  μ={sp['max_sharpe']['mu']:.1%}  SR={sp['max_sharpe']['sharpe']:.2f}")
    print(f"γ*=2.5:     σ={sp['gamma_star']['sigma']:.1%}  μ={sp['gamma_star']['mu']:.1%}  equity={sp['gamma_star']['equity_pct']}%")
