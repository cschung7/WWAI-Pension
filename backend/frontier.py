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

    # Keep only ETFs that start within 30 weeks of the median start date
    start_dates = prices.apply(lambda col: col.first_valid_index())
    median_start = start_dates.sort_values().iloc[len(start_dates) // 2]
    cutoff_start = median_start + pd.Timedelta(weeks=30)
    keep = start_dates[start_dates <= cutoff_start].index
    prices = prices[keep]

    prices = prices.ffill(limit=3)
    prices = prices.dropna()
    prices = prices.dropna(axis=1)

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


def _max_drawdown_vec(rets_np: np.ndarray, W: np.ndarray) -> np.ndarray:
    """
    Vectorised max-drawdown over K portfolios.
    rets_np : (T, n) weekly returns
    W       : (K, n) portfolio weights
    returns : (K,)  max drawdown values (≤ 0)
    """
    port_rets = rets_np @ W.T                               # (T, K)
    equity    = np.cumprod(1.0 + port_rets, axis=0)        # (T, K)
    peak      = np.maximum.accumulate(equity, axis=0)
    dd        = (equity - peak) / np.maximum(peak, 1e-10)
    return dd.min(axis=0)                                   # (K,)


def _pareto_front_2d(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """
    Maximize both x and y.  O(n log n) sort + sweep.
    Returns boolean mask of non-dominated points.
    """
    n = len(x)
    if n == 0:
        return np.zeros(0, dtype=bool)
    order  = np.argsort(-x)     # descending x
    mask   = np.zeros(n, dtype=bool)
    best_y = -np.inf
    for idx in order:
        if y[idx] > best_y:
            mask[idx] = True
            best_y = y[idx]
    return mask


def _portfolio_dd_calmar(w: np.ndarray, rets_np: np.ndarray, ann_mu: float) -> tuple[float, float]:
    """MaxDD and Calmar for a single weight vector."""
    port_rets = rets_np @ w
    equity    = np.cumprod(1.0 + port_rets)
    peak      = np.maximum.accumulate(equity)
    dd        = (equity - peak) / np.maximum(peak, 1e-10)
    max_dd    = float(dd.min())
    calmar    = ann_mu / abs(max_dd) if max_dd < -1e-6 else 0.0
    return round(max_dd, 4), round(calmar, 3)


# ─────────────────────────────────────────────────────────────────────────────
# Monte Carlo
# ─────────────────────────────────────────────────────────────────────────────

def run_monte_carlo(
    mu: np.ndarray,
    cov: np.ndarray,
    is_equity: np.ndarray,
    is_safe: np.ndarray,
    weeks: int,
    rets_df: pd.DataFrame,
    n: int = 10_000,
) -> list[dict]:
    rng      = np.random.default_rng(42)
    n_assets = len(mu)
    rets_np  = rets_df.values.astype(np.float64)   # (T, n_assets)

    # ── Generate FSC-clipped weight matrix ──────────────────────────────────
    W = np.zeros((n, n_assets))
    for i in range(n):
        raw   = rng.dirichlet(np.ones(n_assets))
        W[i]  = _fsc_clip(raw, is_equity, is_safe)

    # ── Vectorised portfolio stats ───────────────────────────────────────────
    ann_mu_p  = W @ (mu * weeks)                    # (n,)
    WC        = W @ (cov * weeks)                   # (n, n_assets)
    ann_var_p = (WC * W).sum(axis=1)               # (n,)
    ann_sig_p = np.sqrt(np.maximum(ann_var_p, 1e-10))
    sharpe_p  = (ann_mu_p - RF_RATE) / ann_sig_p   # (n,)

    # ── Vectorised max-drawdown + Calmar ─────────────────────────────────────
    max_dd_p = _max_drawdown_vec(rets_np, W)        # (n,)
    calmar_p = np.where(max_dd_p < -1e-6,
                        ann_mu_p / np.abs(max_dd_p),
                        0.0)                        # (n,)

    # ── Pareto fronts ────────────────────────────────────────────────────────
    # Sharpe vs MaxDD  : maximize Sharpe (y) and MaxDD (x, closer to 0 = better)
    p_msdd     = _pareto_front_2d(max_dd_p, sharpe_p)
    # Sharpe vs Calmar : maximize both
    p_mscalmar = _pareto_front_2d(calmar_p, sharpe_p)

    results = []
    for i in range(n):
        results.append({
            "sigma":      round(float(ann_sig_p[i]), 4),
            "mu":         round(float(ann_mu_p[i]), 4),
            "sharpe":     round(float(sharpe_p[i]), 3),
            "max_dd":     round(float(max_dd_p[i]), 4),
            "calmar":     round(float(calmar_p[i]), 3),
            "p_msdd":     bool(p_msdd[i]),
            "p_mscalmar": bool(p_mscalmar[i]),
        })

    # ── Downsample to 3 000: always keep Pareto points ──────────────────────
    pareto_idx = [i for i, r in enumerate(results) if r["p_msdd"] or r["p_mscalmar"]]
    other_idx  = [i for i in range(n) if i not in set(pareto_idx)]
    target_other = max(0, 3000 - len(pareto_idx))
    if len(other_idx) > target_other:
        rng2      = np.random.default_rng(99)
        other_idx = rng2.choice(other_idx, size=target_other, replace=False).tolist()
    keep    = set(pareto_idx + other_idx)
    results = [r for i, r in enumerate(results) if i in keep]
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

    eq_constraint   = {"type": "ineq", "fun": lambda w: MAX_EQUITY - w[is_equity].sum()}
    safe_constraint = {"type": "ineq", "fun": lambda w: w[is_safe].sum() - MIN_SAFE}
    sum_constraint  = {"type": "eq",   "fun": lambda w: w.sum() - 1.0}
    bounds = [(0.0, 1.0)] * n

    w0 = np.ones(n) / n
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
        w0 = result.x if result.success else np.ones(n) / n

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
    rets_df: pd.DataFrame,
    gamma: float = 2.5,
) -> dict:
    n       = len(mu)
    ann_mu  = mu * weeks
    ann_cov = cov * weeks
    w0      = np.ones(n) / n
    bounds  = [(0.0, 1.0)] * n
    rets_np = rets_df.values.astype(np.float64)

    base_cons = [
        {"type": "eq",  "fun": lambda w: w.sum() - 1.0},
        {"type": "ineq","fun": lambda w: MAX_EQUITY - w[is_equity].sum()},
        {"type": "ineq","fun": lambda w: w[is_safe].sum() - MIN_SAFE},
    ]

    def _weights_dict(w: np.ndarray, threshold: float = 0.005) -> dict:
        return {tickers[i]: round(float(w[i]), 4) for i in range(n) if w[i] > threshold}

    def _enrich(w: np.ndarray, sig: float, ret: float, sr: float) -> dict:
        max_dd, calmar = _portfolio_dd_calmar(w, rets_np, ret)
        return {
            "sigma": round(sig, 4), "mu": round(ret, 4),
            "sharpe": round(sr, 3), "max_dd": max_dd, "calmar": calmar,
            "weights": _weights_dict(w),
        }

    # MVP
    res_mvp  = minimize(lambda w: w @ ann_cov @ w, w0, method="SLSQP",
                        bounds=bounds, constraints=base_cons,
                        options={"ftol": 1e-9, "maxiter": 500})
    mvp_w    = res_mvp.x if res_mvp.success else w0
    mvp_sig, mvp_mu, mvp_sr = _port_stats(mvp_w, mu, cov, weeks)

    # Max Sharpe
    res_ms = minimize(
        lambda w: -(w @ ann_mu - RF_RATE) / sqrt(max(w @ ann_cov @ w, 1e-10)),
        w0, method="SLSQP",
        bounds=bounds, constraints=base_cons,
        options={"ftol": 1e-9, "maxiter": 500},
    )
    ms_w   = res_ms.x if res_ms.success else w0
    ms_sig, ms_mu, ms_sr = _port_stats(ms_w, mu, cov, weeks)

    # γ* portfolio
    target_eq  = min(1.0 / max(gamma, 0.1), MAX_EQUITY)
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

    gs_result = _enrich(gs_w, gs_sig, gs_mu, gs_sr)
    gs_result["equity_pct"] = round(float(gs_w[is_equity].sum()) * 100, 1)
    gs_result["safe_pct"]   = round(float(gs_w[is_safe].sum()) * 100, 1)

    return {
        "mvp":        _enrich(mvp_w, mvp_sig, mvp_mu, mvp_sr),
        "max_sharpe": _enrich(ms_w,  ms_sig,  ms_mu,  ms_sr),
        "gamma_star": gs_result,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main builder
# ─────────────────────────────────────────────────────────────────────────────

def build_frontier_response(lookback: str = "3y", top_n: int = 100, gamma: float = 2.5) -> dict:
    cache_key = f"{lookback}_{top_n}"
    now = time.time()

    base = _cache.get(cache_key)
    if base and (now - base["ts"]) < CACHE_TTL_SECONDS:
        # Recompute only gamma_star with new gamma (fast)
        tickers = base["tickers"]
        mu_arr  = base["mu_arr"]
        cov_arr = base["cov_arr"]
        is_eq   = base["is_equity"]
        is_safe = base["is_safe"]
        weeks   = base["weeks"]
        rets_df = base["rets_df"]
        specials = special_portfolios(mu_arr, cov_arr, tickers, is_eq, is_safe, weeks, rets_df, gamma)
        resp = dict(base["response"])
        resp["special"] = specials
        resp["meta"]["gamma"] = gamma
        return resp

    t0 = time.time()
    log.info(f"Computing frontier: lookback={lookback} top_n={top_n}")

    universe = _load_universe()
    ucs      = _load_ucs_scores()

    universe["ucs_score"] = universe["ticker"].map(ucs).fillna(0.0)
    universe = universe[universe["ticker"].apply(
        lambda t: (PRICE_DIR / f"{t}.csv").exists()
    )].copy()
    universe = universe.nlargest(top_n, "ucs_score").reset_index(drop=True)

    tickers      = universe["ticker"].tolist()
    asset_classes = universe.set_index("ticker")["asset_class"].to_dict()

    ret_df = load_returns(tickers, lookback)
    if ret_df.empty or len(ret_df.columns) < 5:
        raise ValueError("Insufficient return data")

    valid_tickers  = ret_df.columns.tolist()
    n              = len(valid_tickers)
    weeks_per_year = 52
    weeks_actual   = len(ret_df)

    mu_arr  = ret_df.mean().values
    lw = LedoitWolf()
    lw.fit(ret_df.values)
    cov_arr = lw.covariance_

    ac       = [asset_classes.get(t, "domestic_equity") for t in valid_tickers]
    is_equity = np.array([c in EQUITY_CLASSES for c in ac])
    is_safe   = np.array([c in SAFE_CLASSES   for c in ac])

    log.info(f"  {n} ETFs, {weeks_actual} weekly obs — running Monte Carlo…")
    mc = run_monte_carlo(mu_arr, cov_arr, is_equity, is_safe, weeks_per_year, ret_df)

    log.info("  Monte Carlo done — computing analytical frontier…")
    curve = compute_frontier(mu_arr, cov_arr, is_equity, is_safe, weeks_per_year)

    log.info("  Frontier done — computing special portfolios…")
    specials = special_portfolios(mu_arr, cov_arr, valid_tickers, is_equity, is_safe,
                                   weeks_per_year, ret_df, gamma)

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
            "n_etfs":        n,
            "lookback":      lookback,
            "lookback_days": {"1y": 365, "2y": 730, "3y": 1095}[lookback],
            "weeks":         weeks_actual,
            "computed_at":   datetime.now().isoformat(),
            "compute_sec":   elapsed,
            "rf_rate":       RF_RATE,
            "gamma":         gamma,
        },
    }

    _cache[cache_key] = {
        "ts": now, "response": response,
        "tickers": valid_tickers,
        "mu_arr": mu_arr, "cov_arr": cov_arr,
        "is_equity": is_equity, "is_safe": is_safe,
        "weeks": weeks_per_year,
        "rets_df": ret_df,
    }
    return response


# ── Standalone test ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t0 = time.time()
    resp = build_frontier_response("3y", 60, 2.5)
    m  = resp["meta"]
    sp = resp["special"]
    mc = resp["monte_carlo"]
    print(f"\n=== korea-roboAdvisor-etf-frontier ===")
    print(f"ETFs: {m['n_etfs']}  weeks: {m['weeks']}  computed in {m['compute_sec']}s")
    print(f"Frontier points: {len(resp['frontier_curve'])}  MC points: {len(mc)}")
    print(f"MVP:        σ={sp['mvp']['sigma']:.1%}  μ={sp['mvp']['mu']:.1%}  SR={sp['mvp']['sharpe']:.2f}  MaxDD={sp['mvp']['max_dd']:.1%}  Cal={sp['mvp']['calmar']:.2f}")
    print(f"Max-Sharpe: σ={sp['max_sharpe']['sigma']:.1%}  μ={sp['max_sharpe']['mu']:.1%}  SR={sp['max_sharpe']['sharpe']:.2f}  MaxDD={sp['max_sharpe']['max_dd']:.1%}  Cal={sp['max_sharpe']['calmar']:.2f}")
    print(f"γ*=2.5:     σ={sp['gamma_star']['sigma']:.1%}  μ={sp['gamma_star']['mu']:.1%}  equity={sp['gamma_star']['equity_pct']}%  MaxDD={sp['gamma_star']['max_dd']:.1%}")
    n_p1 = sum(1 for r in mc if r['p_msdd'])
    n_p2 = sum(1 for r in mc if r['p_mscalmar'])
    print(f"Pareto (Sharpe-MaxDD): {n_p1}  Pareto (Sharpe-Calmar): {n_p2}")
