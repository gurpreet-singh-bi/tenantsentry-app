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
    r"^\s*(\d+)\.\s+[A-Z][A-Z\s]{3,}",                   # "1. RENT REVIEW"
    r"^\s*(\d+\.\d+)\s+[A-Z][A-Za-z\s]{3,}",             # "2.1 Definitions"
    r"^\s*(\d+\.\d+\.\d+)\s+[A-Z][A-Za-z\s]{2,}",      # "2.1.1 Sub-clause"
    r"^\s*(\d+\.\d+\.\d+\.\d+)\s+[A-Z][A-Za-z\s]{2,}", # "2.1.1.1 Deep sub-clause"
    r"^\s*CLAUSE\s+\d+",                                   # "CLAUSE 5"
    r"^\s*PART\s+[IVXLC]+",                                # "PART III"
    r"^\s*SCHEDULE\s+\d+",                                 # "SCHEDULE 1"
]

COMPILED_PATTERNS = [re.compile(p, re.MULTILINE) for p in CLAUSE_HEADING_PATTERNS]


@dataclass
class Chunk:
    content: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.metadata.setdefault("chunk_type", "lease")


def _is_toc_page(text: str) -> bool:
    """
    Return True if this page is predominantly a Table of Contents.

    TOC lines have the pattern: "Some Heading .......  12"
    (heading text, then 5+ dots, then a page number).
    If >50% of non-blank lines match that pattern the page is a TOC page
    and should be excluded from clause chunking — otherwise the dotted
    leaders become the "body" of every matched heading.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 3:
        return False
    toc_line = re.compile(r'.+\.{5,}\s*\d+\s*$')
    toc_count = sum(1 for l in lines if toc_line.match(l))
    return toc_count / len(lines) > 0.5


def chunk_document(pages: list[dict], document_metadata: dict) -> list[Chunk]:
    """
    Takes parsed pages from pdf_parser and returns clause-level chunks.

    Args:
        pages: List of {page_num, text} dicts from ParsedDocument.pages
        document_metadata: Dict with keys like {jurisdiction, tenant_name, ...}

    Returns:
        List of Chunk objects ready for embedding
    """
    # Strip Table of Contents pages before chunking.
    # TOC pages contain dotted leaders (e.g. "2.1 Lease ........... 7") that
    # match clause heading patterns but produce empty/dotted bodies.  Without
    # this filter, Haiku triage flags those near-empty TOC chunks instead of
    # the real body clauses, causing "CLAUSE TEXT NOT PROVIDED" on every flag.
    body_pages = [p for p in pages if not _is_toc_page(p.get("text", ""))]
    toc_skipped = len(pages) - len(body_pages)
    if toc_skipped:
        logger.info(f"Skipped {toc_skipped} TOC page(s) before chunking")

    full_text = "\n".join(p["text"] for p in body_pages)
    raw_chunks = _split_on_clause_headings(full_text)

    # Claude's context window is large but we cap clause chunks at 6000 chars
    # (~1500 tokens) to keep prompts predictable and avoid token-limit failures.
    # Longer clauses are almost always bloated boilerplate; the key terms appear
    # in the first few hundred chars.
    MAX_CHUNK_CHARS = 6000

    chunks = []
    for i, (heading, body) in enumerate(raw_chunks):
        content = f"{heading}\n{body}".strip()
        if len(content) < 50:   # skip boilerplate/blank chunks
            continue

        if len(content) > MAX_CHUNK_CHARS:
            logger.debug(
                f"Chunk {i} truncated {len(content)} → {MAX_CHUNK_CHARS} chars "
                f"(heading: {heading[:60]})"
            )
            content = content[:MAX_CHUNK_CHARS] + "\n[...truncated — clause continues in document...]"

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
