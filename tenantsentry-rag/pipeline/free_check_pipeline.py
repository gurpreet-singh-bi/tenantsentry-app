"""
free_check_pipeline.py — REMOVED 2026-06-07
--------------------------------------------
This module has been deleted as part of the free-check refactor.

The free-check now reuses the real audit pipeline (run_dev_audit / run_audit)
with page truncation (max_pages=5) and skip_vector_store=True.
One high-quality engine for all surfaces — no separate lightweight pipeline.

  DEV:  pipeline.dev_pipeline.run_dev_audit
  LIVE: pipeline.audit_pipeline.run_audit (with max_pages=5, skip_vector_store=True)

Manual entry scoring is now _score_manual_entry() in api/main.py.
Free-check results are logged to the free_check_run Supabase table via db/free_check_store.py.

Do NOT add new code to this file.
"""

raise ImportError(
    "pipeline.free_check_pipeline has been removed. "
    "Use pipeline.dev_pipeline.run_dev_audit (DEV) or "
    "pipeline.audit_pipeline.run_audit with max_pages=5 (LIVE). "
    "See api/main.py:_run_free_check_job for the correct pattern."
)
