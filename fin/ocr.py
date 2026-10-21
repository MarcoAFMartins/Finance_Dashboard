"""Extração de texto: camada de texto do PDF primeiro, OCR (Tesseract) só quando falta."""
from pathlib import Path

import pdfplumber
import pytesseract
from PIL import Image, ImageOps

MIN_CHARS_PER_PAGE = 40  # abaixo disto assume-se página digitalizada


def ocr_lang() -> str:
    try:
        langs = set(pytesseract.get_languages())
    except Exception:
        return "eng"
    return "por+eng" if "por" in langs else "eng"


def ocr_image(img: Image.Image) -> str:
    img = ImageOps.autocontrast(ImageOps.grayscale(img))
    return pytesseract.image_to_string(img, lang=ocr_lang(), config="--psm 6")


def pdf_pages_text(path: Path) -> tuple[list[str], str]:
    """Devolve (texto por página, método: pdf-text | ocr | mixed)."""
    from pdf2image import convert_from_path

    pages: list[str] = []
    used_ocr = used_text = False
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if len(text.strip()) >= MIN_CHARS_PER_PAGE:
                used_text = True
            else:
                img = convert_from_path(str(path), dpi=300, first_page=i + 1, last_page=i + 1)[0]
                text = ocr_image(img)
                used_ocr = True
            pages.append(text)
    method = "mixed" if used_ocr and used_text else "ocr" if used_ocr else "pdf-text"
    return pages, method


def image_text(path: Path) -> tuple[list[str], str]:
    return [ocr_image(Image.open(path))], "ocr"
