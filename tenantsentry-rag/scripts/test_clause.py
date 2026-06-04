"""
test_clause.py
--------------
Diagnostic: sends a hardcoded high-risk clause directly to Claude
and prints the raw response. Run this to confirm the prompt+model
is returning flags before debugging the pipeline.

Usage:
    cd tenantsentry-rag
    python scripts/test_clause.py
"""

import sys, os, json
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from llm.router import analyse_clause

# ── Test clause — personal guarantee (should trigger RF007) ──────────────────
TEST_CLAUSE = """
7.1 The Directors of the Tenant company (Peter Baker and Sam Chen) must provide
an absolute and unconditional personal guarantee, jointly and severally, to secure
the performance of all Tenant covenants, rent payments, and indemnity obligations
under this Lease for the full term of the Lease and any option period exercised.
The guarantee is unlimited in amount and survives termination of the Lease.
"""

# ── Test legislation context (simulating what RAG returns) ───────────────────
TEST_LEGISLATION = """
Australian Commercial Lease — Personal Guarantee Best Practice:
Personal guarantees expose individual directors to unlimited personal liability.
Best practice is to cap guarantees at 6-12 months rent equivalent.
A guarantee should be time-limited (expire after year 2 if no default).
"""

# ── Test rules (subset of red_flags.yaml) ────────────────────────────────────
TEST_RULES = """
Rule RF007 [HIGH]: Personal guarantee without cap
  Description: Directors required to provide unlimited personal guarantees
  over the full lease term without any cap or time limit.
  Trigger keywords: personal guarantee, guarantee, guarantor, indemnify
  Action: Negotiate to cap guarantee at 6-12 months rent equivalent.
"""

print("=" * 60)
print("TenantSentry — Clause Analysis Diagnostic")
print("=" * 60)
print(f"\nClause text:\n{TEST_CLAUSE.strip()}\n")
print("Sending to Claude...\n")

try:
    result = analyse_clause(
        clause_text=TEST_CLAUSE,
        legislation_context=TEST_LEGISLATION,
        rules_context=TEST_RULES,
        jurisdiction="NSW",
    )
    print("Raw result from Claude:")
    print(json.dumps(result, indent=2))

    flags = result.get("risk_flags", [])
    print(f"\n{'✓' if flags else '✗'} Risk flags returned: {len(flags)}")
    if not flags:
        print("\n⚠ PROBLEM: No flags returned for a clear personal guarantee clause.")
        print("Check the raw result above — if 'error' key exists, JSON parsing failed.")
        print("If risk_flags is [] with no error, Claude is not flagging. Check the prompt.")
    else:
        for f in flags:
            print(f"  [{f.get('severity','?').upper()}] {f.get('description','')[:80]}")

except Exception as e:
    print(f"\n✗ Exception: {e}")
    import traceback; traceback.print_exc()
