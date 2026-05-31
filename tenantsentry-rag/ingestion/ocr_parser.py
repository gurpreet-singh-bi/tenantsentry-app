"""
ocr_parser.py
-------------
OCR pipeline for scanned commercial lease PDFs.
Uses pdf2image + pytesseract to extract text from image-based pages.

Requirements:
  - pip install pdf2image pytesseract
  - Tesseract binary must be installed:
      Windows: https://github.com/UB-Mannheim/tesseract/wiki
      macOS:   brew install tesseract
      Linux:   apt-get install tesseract-ocr
  - Set TESSERACT_CMD env var if Tesseract is not on PATH:
      TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe

For production scale, swap pytesseract for AWS Textract (higher accuracy,
handles complex layouts, costs ~$1.50/1000 pages).
"""

import os
from pathlib import Path
from loguru import logger

try:
    import pytesseract
    from pdf2image import convert_from_path
    OCR_AVAILABLE = True

    # Allow override via env var (useful on Windows where Tesseract is not on PATH)
    tesseract_cmd = os.environ.get("TESSERACT_CMD")
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

except ImportError:
    OCR_AVAILABLE = False
    logger.warning("pytesseract or pdf2image not installed — OCR unavailable")


def ocr_pdf(file_path: str, dpi: int = 300) -> list[dict]:
    """
    Extract text from a scanned PDF using OCR.

    Args:
        file_path: Absolute path to the PDF
        dpi: Resolution for image conversion (300 is standard for legal docs)

    Returns:
        List of {page_num, text, is_scanned} dicts (same shape as pdf_parser output)

    Raises:
        RuntimeError: If Tesseract is not installed
        FileNotFoundError: If PDF doesn't exist
    """
    if not OCR_AVAILABLE:
        raise RuntimeError(
            "OCR libraries not installed. Run: pip install pytesseract pdf2image\n"
            "Then install Tesseract: https://github.com/UB-Mannheim/tesseract/wiki"
        )

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {file_path}")

    logger.info(f"Running OCR on {path.name} at {dpi}dpi")

    try:
        # Convert PDF pages to PIL images
        images = convert_from_path(file_path, dpi=dpi)
    except Exception as e:
        raise RuntimeError(f"Failed to convert PDF to images: {e}")

    pages = []
    for i, image in enumerate(images):
        try:
            # OCR the page image — lang='eng' for English text
            text = pytesseract.image_to_string(image, lang="eng", config="--psm 6")
            pages.append({
                "page_num": i + 1,
                "text": text,
                "is_scanned": True,
            })
            logger.debug(f"OCR page {i+1}: {len(text)} chars extracted")
        except Exception as e:
            logger.error(f"OCR failed on page {i+1}: {e}")
            pages.append({
                "page_num": i + 1,
                "text": "",
                "is_scanned": True,
            })

    total_chars = sum(len(p["text"]) for p in pages)
    logger.info(f"OCR complete: {len(pages)} pages, {total_chars} total chars")
    return pages


def is_ocr_available() -> bool:
    """Check if OCR dependencies are installed and functional."""
    if not OCR_AVAILABLE:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False
