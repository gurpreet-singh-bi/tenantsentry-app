"""
pdf_parser.py
-------------
Extracts text from commercial lease PDFs.
Tries pdfplumber first (best for digital PDFs), falls back to PyMuPDF,
then flags scanned docs for OCR routing.
"""

import pdfplumber
import fitz  # PyMuPDF
from pathlib import Path
from dataclasses import dataclass
from loguru import logger


@dataclass
class ParsedDocument:
    file_path: str
    pages: list[dict]       # [{page_num, text, is_scanned}]
    is_scanned: bool
    total_pages: int
    metadata: dict


def parse_pdf(file_path: str) -> ParsedDocument:
    """
    Main entry point. Returns ParsedDocument with extracted text per page.
    Caller should check .is_scanned and route to ocr_parser if True.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {file_path}")

    logger.info(f"Parsing PDF: {path.name}")

    pages = _extract_with_pdfplumber(file_path)

    # Detect if the doc is likely scanned (very little extractable text)
    scanned_pages = [p for p in pages if len(p["text"].strip()) < 50]
    is_scanned = len(scanned_pages) / max(len(pages), 1) > 0.5

    if is_scanned:
        logger.warning(f"{path.name} appears to be scanned — route to OCR")

    return ParsedDocument(
        file_path=file_path,
        pages=pages,
        is_scanned=is_scanned,
        total_pages=len(pages),
        metadata={"filename": path.name, "file_size_kb": path.stat().st_size // 1024}
    )


def _extract_with_pdfplumber(file_path: str) -> list[dict]:
    pages = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                pages.append({
                    "page_num": i + 1,
                    "text": text,
                    "is_scanned": len(text.strip()) < 50
                })
    except Exception as e:
        logger.error(f"pdfplumber failed: {e}, trying PyMuPDF")
        pages = _extract_with_pymupdf(file_path)
    return pages


def _extract_with_pymupdf(file_path: str) -> list[dict]:
    pages = []
    doc = fitz.open(file_path)
    for i, page in enumerate(doc):
        text = page.get_text()
        pages.append({
            "page_num": i + 1,
            "text": text,
            "is_scanned": len(text.strip()) < 50
        })
    doc.close()
    return pages
