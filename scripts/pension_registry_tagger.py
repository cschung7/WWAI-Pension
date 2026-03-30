"""
One-time script: tag existing pension runs in portfolio.db with risk-tier labels.
Run once: python3 scripts/pension_registry_tagger.py

Safe to re-run (idempotent — overwrites existing tags).
"""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path("/mnt/nas/WWAI/WWAI-ETF-FABLESS/etf-fabless-framework/results/portfolio.db")

# Tier classification: (min_ann_return, max_dd_floor) → tier label
# max_dd_floor = most negative DD allowed for this tier
TIER_RULES = [
    # experiment substring → tags to assign
    # FSC-compliant variants (bond 30% forced)
    ("pension_s35_S3.5+IB",   ["pension","fsc_compliant","tier:T2","bond_pct:30","strategy:s35ib","regime:semiai_defense"]),
    ("pension_s35_S3.5+B",    ["pension","fsc_compliant","tier:T2","bond_pct:30","strategy:s35b","regime:semiai_defense"]),
    # Non-FSC variants (no forced bond)
    ("pension_s35_S4-ref",    ["pension","tier:T4","bond_pct:0","strategy:s4","regime:semiai_defense"]),
    ("pension_s35_S3.5",      ["pension","tier:T3","bond_pct:0","strategy:s35","regime:semiai_defense"]),
    ("pension_s35_S3.5+I",    ["pension","tier:T3","bond_pct:0","strategy:s35i","regime:semiai_defense"]),
    # Distillation series
    ("pension_distillation_TEACHER",      ["pension","tier:T3","bond_pct:0","strategy:teacher","regime:semiai_defense"]),
    ("pension_distillation_S4-mom-rot",   ["pension","tier:T4","bond_pct:0","strategy:s4_momrot","regime:semiai_defense"]),
    ("pension_distillation_S3-rotate",    ["pension","tier:T3","bond_pct:0","strategy:s3_rotate","regime:semiai_defense"]),
    ("pension_distillation_S2-blend50",   ["pension","tier:T2","bond_pct:50","strategy:s2_blend50","regime:semiai_defense"]),
    ("pension_distillation_S1-blend30",   ["pension","tier:T1","bond_pct:30","strategy:s1_blend30","regime:semiai_defense"]),
    ("pension_distillation_S0-base",      ["pension","tier:T2","bond_pct:0","strategy:s0_base","regime:semiai_defense"]),
    ("pension_distillation_S5-rot+mom",   ["pension","tier:T2","bond_pct:0","strategy:s5_rotmom","regime:semiai_defense"]),
    # EW baseline series (Mode 1/2)
    ("pension_semi_ai_baseline",          ["pension","tier:T2","bond_pct:0","strategy:m1_baseline","regime:semiai_defense"]),
    ("pension_semi_ai_A",                 ["pension","tier:T2","bond_pct:0","strategy:m2_A","regime:semiai_defense"]),
    ("pension_semi_ai_B",                 ["pension","tier:T2","bond_pct:0","strategy:m2_B","regime:semiai_defense"]),
    ("pension_semi_ai_C",                 ["pension","tier:T2","bond_pct:0","strategy:m2_C","regime:semiai_defense"]),
    ("pension_semi_ai_D",                 ["pension","fsc_adjacent","tier:T2","bond_pct:0","strategy:m2_D","regime:semiai_defense"]),
    ("pension_semi_ai_E",                 ["pension","tier:T2","bond_pct:0","strategy:m2_E","regime:semiai_defense"]),
    ("pension_semi_ai_F",                 ["pension","tier:T2","bond_pct:0","strategy:m2_F","regime:semiai_defense"]),
    ("pension_semi_ai_G",                 ["pension","tier:T2","bond_pct:0","strategy:m2_G","regime:semiai_defense"]),
]

def derive_tier_from_metrics(ann_return_pct: float, max_dd_pct: float) -> str:
    """Auto-derive tier if not explicitly tagged."""
    dd = abs(max_dd_pct)
    r = ann_return_pct
    if r >= 35:
        return "T4"
    if r >= 25 and dd <= 25:
        return "T3"
    if r >= 15 and dd <= 18:
        return "T2"
    return "T1"


def main():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Fetch all pension runs
    cur.execute("""
        SELECT id, experiment, product, ann_return_pct, max_dd_pct, tags
        FROM runs
        WHERE product LIKE 'Pension-%'
        ORDER BY id
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} pension runs to tag\n")

    updated = 0
    for run_id, experiment, product, ann_ret, max_dd, tags_raw in rows:
        # Find matching tag rule
        matched_tags = None
        for pattern, tags in TIER_RULES:
            if pattern in (experiment or ""):
                matched_tags = tags
                break

        if matched_tags is None:
            # Auto-derive
            tier = derive_tier_from_metrics(ann_ret or 0, max_dd or 0)
            matched_tags = ["pension", f"tier:{tier}", "regime:semiai_defense"]
            print(f"  AUTO-DERIVED id={run_id} exp={experiment}: {tier}")

        # Check if FSC compliant: bond_pct:30 in tags
        bond_pct = 0
        for t in matched_tags:
            if t.startswith("bond_pct:"):
                bond_pct = int(t.split(":")[1])

        # Add fsc_compliant if bond >= 30 and not already tagged
        if bond_pct >= 30 and "fsc_compliant" not in matched_tags:
            matched_tags = ["fsc_compliant"] + matched_tags

        cur.execute("UPDATE runs SET tags=? WHERE id=?", (json.dumps(matched_tags), run_id))
        print(f"  TAGGED id={run_id:4d} | {product:30s} | {experiment:40s} | {json.dumps(matched_tags)}")
        updated += 1

    conn.commit()
    conn.close()
    print(f"\n✅ Tagged {updated} runs in {DB_PATH}")


if __name__ == "__main__":
    main()
