"""
test_chunker.py
---------------
Unit tests for the section-aware chunker.
Run with: pytest tests/test_chunker.py -v
"""

import pytest
from ingestion.chunker import chunk_document, _split_on_clause_headings


SAMPLE_LEASE_TEXT = """
COMMERCIAL LEASE AGREEMENT

1. DEFINITIONS AND INTERPRETATION
In this Lease unless the context otherwise requires:
"Commencement Date" means 1 July 2024.
"Term" means 3 years from the Commencement Date.

2. GRANT OF LEASE
The Landlord leases the Premises to the Tenant for the Term on the
conditions set out in this Lease.

2.1 Rent
The Tenant must pay the annual rent of $120,000 plus GST per annum,
payable monthly in advance on the first day of each month.

3. RENT REVIEW
3.1 The rent shall be reviewed annually on each anniversary of the
Commencement Date by a fixed increase of 4% per annum.
"""

SAMPLE_PAGES = [{"page_num": 1, "text": SAMPLE_LEASE_TEXT, "is_scanned": False}]
SAMPLE_METADATA = {"jurisdiction": "NSW", "tenant_name": "Test Pty Ltd"}


def test_splits_on_numbered_clauses():
    chunks = chunk_document(SAMPLE_PAGES, SAMPLE_METADATA)
    headings = [c.metadata["clause_heading"] for c in chunks]
    assert any("DEFINITIONS" in h or "1." in h for h in headings)
    assert any("GRANT" in h or "2." in h for h in headings)


def test_minimum_chunk_count():
    chunks = chunk_document(SAMPLE_PAGES, SAMPLE_METADATA)
    assert len(chunks) >= 2, "Should produce at least 2 chunks from sample lease"


def test_metadata_attached():
    chunks = chunk_document(SAMPLE_PAGES, SAMPLE_METADATA)
    for chunk in chunks:
        assert chunk.metadata["jurisdiction"] == "NSW"
        assert chunk.metadata["chunk_type"] == "lease"
        assert "chunk_index" in chunk.metadata


def test_no_empty_chunks():
    chunks = chunk_document(SAMPLE_PAGES, SAMPLE_METADATA)
    for chunk in chunks:
        assert len(chunk.content.strip()) > 0


def test_rent_review_clause_captured():
    chunks = chunk_document(SAMPLE_PAGES, SAMPLE_METADATA)
    combined = " ".join(c.content for c in chunks)
    assert "4%" in combined or "rent review" in combined.lower()
