"""
chunker.py
----------
Section-aware chunking for commercial lease documents.
Splits on clause headings (e.g. "1.", "2.1", "CLAUSE 5") rather than
fixed token counts — preserves the natural unit of meaning in leases.
"""

import re
from dataclasses import dataclass, field
from loguru import logger


# Regex patterns that indicate a new clause/section heading in AU commercial leases
CLAUSE_HEADING_PATTERNS = [
    r"^\s*(\d+)\.\s+[A-Z][A-Z\s]{3,}",           # "1. RENT REVIEW"
    r"^\s*(\d+\.\d+)\s+[A-Z][A-Za-z\s]{3,}",     # "2.1 Definitions"
    r"^\s*CLAUSE\s+\d+",                           # "CLAUSE 5"
    r"^\s*PART\s+[IVXLC]+",                        # "PART III"
    r"^\s*SCHEDULE\s+\d+",                         # "SCHEDULE 1"
]

COMPILED_PATTERNS = [re.compile(p, re.MULTILINE) for p in CLAUSE_HEADING_PATTERNS]


@dataclass
class Chunk:
    content: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.metadata.setdefault("chunk_type", "lease")


def chunk_document(pages: list[dict], document_metadata: dict) -> list[Chunk]:
    """
    Takes parsed pages from pdf_parser and returns clause-level chunks.

    Args:
        pages: List of {page_num, text} dicts from ParsedDocument.pages
        document_metadata: Dict with keys like {jurisdiction, tenant_name, ...}

    Returns:
        List of Chunk objects ready for embedding
    """
    full_text = "\n".join(p["text"] for p in pages)
    raw_chunks = _split_on_clause_headings(full_text)

    chunks = []
    for i, (heading, body) in enumerate(raw_chunks):
        content = f"{heading}\n{body}".strip()
        if len(content) < 50:   # skip boilerplate/blank chunks
            continue

        chunks.append(Chunk(
            content=content,
            metadata={
                **document_metadata,
                "chunk_type": "lease",
                "clause_heading": heading.strip(),
                "chunk_index": i,
                "char_count": len(content),
            }
        ))

    logger.info(f"Chunked into {len(chunks)} clause-level chunks")
    return chunks


def _split_on_clause_headings(text: str) -> list[tuple[str, str]]:
    """
    Finds all clause heading positions and splits text between them.
    Returns list of (heading, body) tuples.
    """
    # Find all heading match positions
    heading_positions = []
    for pattern in COMPILED_PATTERNS:
        for match in pattern.finditer(text):
            # Take only the first line of the match to prevent
            # the heading from bleeding into the next line via \s matching \n
            heading_text = match.group().split("\n")[0].strip()
            heading_positions.append((match.start(), heading_text))

    if not heading_positions:
        # No headings found — fall back to paragraph splitting
        logger.warning("No clause headings detected, falling back to paragraph split")
        return _paragraph_fallback(text)

    # Sort by position and deduplicate overlapping matches
    heading_positions.sort(key=lambda x: x[0])
    heading_positions = _deduplicate_headings(heading_positions)

    # Build chunks
    chunks = []
    for i, (pos, heading) in enumerate(heading_positions):
        start = pos + len(heading)
        end = heading_positions[i + 1][0] if i + 1 < len(heading_positions) else len(text)
        body = text[start:end].strip()
        chunks.append((heading, body))

    return chunks


def _deduplicate_headings(positions: list[tuple]) -> list[tuple]:
    """Remove headings that overlap with previous match."""
    result = []
    last_end = -1
    for pos, heading in positions:
        if pos > last_end:
            result.append((pos, heading))
            last_end = pos + len(heading)
    return result


def _paragraph_fallback(text: str) -> list[tuple[str, str]]:
    """Split by double newline when no clause headings are found."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return [("", p) for p in paragraphs]
