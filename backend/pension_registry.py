"""
Pension Registry API — reads portfolio.db (read-only) and returns
FSC-tagged pension strategy metrics for the registry dashboard.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()

DB_PATH = Path("/mnt/nas/WWAI/WWAI-ETF-FABLESS/etf-fabless-framework/results/portfolio.db")

TIER_META = {
    "T1": {"label": "안심형",  "label_en": "Conservative", "color": "#10b981", "min_cagr": 7,  "max_dd": -10},
    "T2": {"label": "균형형",  "label_en": "Balanced",     "color": "#3b82f6", "min_cagr": 15, "max_dd": -18},
    "T3": {"label": "성장형",  "label_en": "Growth",       "color": "#f59e0b", "min_cagr": 25, "max_dd": -25},
    "T4": {"label": "적극형",  "label_en": "Aggressive",   "color": "#ef4444", "min_cagr": 35, "max_dd": -99},
}

# Product-type categories (orthogonal to tier)
PRODUCT_TYPE_META = {
    "portfolio": {
        "label": "포트폴리오형",
        "label_en": "Portfolio",
        "color": "#6366f1",
        "desc": "다수 ETF 레짐-강화 전략 + 채권 슬리브",
        "desc_en": "Multi-ETF regime-enhanced portfolio + bond sleeve",
    },
    "single_etf_hybrid": {
        "label": "단일ETF 혼합형",
        "label_en": "Single ETF Hybrid",
        "color": "#8b5cf6",
        "desc": "단일 ETF + 채권30% 슬리브 → FSC IRP/DC 충족",
        "desc_en": "Single ETF + 30% bond sleeve → FSC IRP/DC compliant",
    },
    "self_compliant": {
        "label": "자체충족형",
        "label_en": "Self-Compliant",
        "color": "#14b8a6",
        "desc": "ETF 내부에 채권 포함 → 단독으로 FSC IRP/DC 충족",
        "desc_en": "ETF already holds bonds internally → FSC IRP/DC compliant standalone",
    },
}

STRATEGY_LABELS = {
    "s35ib":      "S3.5+IB (Bond30%+Inertia) ★",
    "s35b":       "S3.5+B (Bond30%)",
    "s35i":       "S3.5+I (Inertia)",
    "s35":        "S3.5 (MomRot)",
    "s4":         "S4 (MomRot tight)",
    "teacher":    "Teacher EW (방산/원자력)",
    "s4_momrot":  "S4 MomRot (Distillation)",
    "s3_rotate":  "S3 Rotate",
    "s2_blend50": "S2 Blend-50%",
    "s1_blend30": "S1 Blend-30%",
    "s0_base":    "S0 Base",
    "s5_rotmom":  "S5 Rot+Mom",
    "m1_baseline":"M1 Baseline EW",
    "m2_A":       "M2-A",
    "m2_B":       "M2-B",
    "m2_C":       "M2-C",
    "m2_D":       "M2-D (RegimeGate)",
    "m2_E":       "M2-E",
    "m2_F":       "M2-F",
    "m2_G":       "M2-G",
    "kosdaq_hrp_b30": "KOSDAQ-HRP-B30 (Bond30%)",
    "kosdaq_hrp_b40": "KOSDAQ-HRP-B40 (Bond40%)",
    # AEGIS Multi-Market Pension Series
    "india_active_b30":  "인도Active+Bond30% (AEGIS) ★",
    "japan_active_b30":  "일본Active+Bond30% (AEGIS) ★",
    "usa_active_b30":    "미국Active+Bond30% (AEGIS) ★",
    "hk_active_b30":     "홍콩Active+Bond30% (AEGIS)",
    "china_active_b30":  "중국Active+Bond30% (AEGIS)",
    "krx_active_b30":   "한국Active+Bond30% (AEGIS) ★",
    "krx_active_b40":   "한국Active+Bond40% (AEGIS)",
    # Weekly ETF Top-5 Pipeline (dynamic — market_etf_top5_bXX)
    "krx_etf_top5_b30":    "한국ETF Top5+Bond30% (주간)",
    "usa_etf_top5_b30":    "미국ETF Top5+Bond30% (주간)",
    "japan_etf_top5_b30":  "일본ETF Top5+Bond30% (주간)",
    "china_etf_top5_b30":  "중국ETF Top5+Bond30% (주간)",
    "krx_etf_top5_b40":    "한국ETF Top5+Bond40% (주간)",
    "usa_etf_top5_b40":    "미국ETF Top5+Bond40% (주간)",
    "japan_etf_top5_b40":  "일본ETF Top5+Bond40% (주간)",
    "china_etf_top5_b40":  "중국ETF Top5+Bond40% (주간)",
    # Single ETF + Bond Sleeve (Hybrid) — 단일 ETF + 채권30% 슬리브
    "etf_490490_b30": "SOL 미국배당미국채혼합50+Bond30% ★ (단일ETF)",
    "etf_490490_b20": "SOL 미국배당미국채혼합50+Bond20% (단일ETF)",
    "etf_402970_b30": "ACE 미국배당다우존스+Bond30% ★ (단일ETF)",
    "etf_402970_b40": "ACE 미국배당다우존스+Bond40% (단일ETF)",
    "etf_489250_b30": "KODEX 미국배당다우존스+Bond30% ★ (단일ETF)",
    "etf_489250_b40": "KODEX 미국배당다우존스+Bond40% (단일ETF)",
    "etf_284430_b30": "KODEX 200미국채혼합+Bond30% (단일ETF)",
    "etf_269530_b30": "PLUS S&P글로벌인프라+Bond30% (단일ETF)",
    "etf_379800_b30": "KODEX 미국S&P500+Bond30% (단일ETF)",
    "etf_133690_b30": "TIGER 미국나스닥100+Bond30% (단일ETF)",
}


def _get_conn():
    """Open read-only connection to portfolio.db."""
    uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _parse_tags(tags_raw: str) -> dict:
    """Parse tags JSON array into a dict with extracted fields."""
    try:
        tags = json.loads(tags_raw or "[]")
    except Exception:
        tags = []

    result = {
        "tags": tags,
        "fsc_compliant": "fsc_compliant" in tags,
        "tier": "T2",
        "bond_pct": 0,
        "strategy": "",
        "regime": "",
        "product_type": "portfolio",   # default
    }
    for t in tags:
        if t.startswith("tier:"):
            result["tier"] = t.split(":")[1]
        elif t.startswith("bond_pct:"):
            result["bond_pct"] = int(t.split(":")[1])
        elif t.startswith("strategy:"):
            result["strategy"] = t.split(":")[1]
        elif t.startswith("regime:"):
            result["regime"] = t.split(":")[1]
        elif t.startswith("product_type:"):
            result["product_type"] = t.split(":", 1)[1]
    return result


def _fetch_pension_runs(fsc_only: bool = False) -> list[dict]:
    """Fetch all pension-tagged runs from portfolio.db."""
    if not DB_PATH.exists():
        return []

    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, product, experiment, description,
                   ann_return_pct, max_dd_pct, calmar, sharpe,
                   turnover_avg_pct, tags, timestamp
            FROM runs
            WHERE json_extract(tags, '$') LIKE '%pension%'
              AND ann_return_pct IS NOT NULL
            ORDER BY calmar DESC
        """)
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return []

    results = []
    for row in rows:
        (run_id, product, experiment, description,
         ann_ret, max_dd, calmar, sharpe,
         turnover, tags_raw, ts) = row

        parsed = _parse_tags(tags_raw)
        if fsc_only and not parsed["fsc_compliant"]:
            continue

        tier = parsed["tier"]
        tier_info = TIER_META.get(tier, TIER_META["T2"])
        strategy_key = parsed["strategy"]
        strategy_label = STRATEGY_LABELS.get(strategy_key, experiment or product)
        product_type = parsed["product_type"]
        pt_info = PRODUCT_TYPE_META.get(product_type, PRODUCT_TYPE_META["portfolio"])

        results.append({
            "run_id":              run_id,
            "product":             product,
            "experiment":          experiment,
            "strategy":            strategy_key,
            "strategy_label":      strategy_label,
            "cagr":                round(ann_ret or 0, 2),
            "maxdd":               round(max_dd or 0, 2),
            "calmar":              round(calmar or 0, 3),
            "sharpe":              round(sharpe or 0, 3),
            "turnover":            round(turnover or 0, 1),
            "bond_pct":            parsed["bond_pct"],
            "fsc_compliant":       parsed["fsc_compliant"],
            "tier":                tier,
            "tier_label":          tier_info["label"],
            "tier_label_en":       tier_info["label_en"],
            "tier_color":          tier_info["color"],
            "product_type":        product_type,
            "product_type_label":  pt_info["label"],
            "product_type_label_en": pt_info["label_en"],
            "product_type_color":  pt_info["color"],
            "product_type_desc":   pt_info["desc"],
            "regime":              parsed["regime"],
            "tags":                parsed["tags"],
            "backtest_start":      "2022-01-01",
            "backtest_end":        "2026-03-30",
            "period_years":        4.2,
            "has_equity":          True,
        })

    return results


@router.get("/data")
async def registry_data(fsc_only: bool = False):
    """Return all pension runs with metrics and tier classification."""
    runs = _fetch_pension_runs(fsc_only=fsc_only)
    return JSONResponse(content={"runs": runs, "count": len(runs)})


@router.get("/summary")
async def registry_summary():
    """Return tier counts, best-per-tier, scatter data."""
    runs = _fetch_pension_runs()

    tier_counts = {}
    best_per_tier: dict[str, dict] = {}
    scatter_points = []
    fsc_count = 0

    for r in runs:
        tier = r["tier"]
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if r["fsc_compliant"]:
            fsc_count += 1

        # Best per tier = highest Calmar
        if tier not in best_per_tier or r["calmar"] > best_per_tier[tier]["calmar"]:
            best_per_tier[tier] = r

        scatter_points.append({
            "x": r["maxdd"],
            "y": r["cagr"],
            "size": max(r["calmar"] * 15, 8),
            "color": r["tier_color"],
            "tier": r["tier"],
            "tier_label": r["tier_label"],
            "label": r["strategy_label"],
            "fsc": r["fsc_compliant"],
            "run_id": r["run_id"],
            "calmar": r["calmar"],
            "sharpe": r["sharpe"],
            "bond_pct": r["bond_pct"],
        })

    # Product-type breakdown
    pt_counts: dict[str, int] = {}
    for r in runs:
        pt = r["product_type"]
        pt_counts[pt] = pt_counts.get(pt, 0) + 1

    return JSONResponse(content={
        "total": len(runs),
        "fsc_count": fsc_count,
        "tier_counts": tier_counts,
        "best_per_tier": best_per_tier,
        "scatter": scatter_points,
        "tier_meta": TIER_META,
        "product_type_counts": pt_counts,
        "product_type_meta": PRODUCT_TYPE_META,
    })


@router.get("/{run_id}/equity")
async def run_equity(run_id: int):
    """Return equity curve for a specific run (for modal chart)."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="portfolio.db not found")

    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT equity_curve, product, experiment, ann_return_pct, max_dd_pct, calmar, sharpe FROM runs WHERE id=?",
            (run_id,)
        )
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not row:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    equity_raw, product, experiment, ann_ret, max_dd, calmar, sharpe = row
    try:
        equity = json.loads(equity_raw or "[]")
    except Exception:
        equity = []

    return JSONResponse(content={
        "run_id": run_id,
        "product": product,
        "experiment": experiment,
        "cagr": round(ann_ret or 0, 2),
        "maxdd": round(max_dd or 0, 2),
        "calmar": round(calmar or 0, 3),
        "sharpe": round(sharpe or 0, 3),
        "equity": equity,
    })
