"""Conversion engine for conversordepdf.pro.

One request = one conversion. Input and output live in a private temp folder that is
deleted as soon as the response is built, so no user file is ever stored.
All user-facing error messages are in Portuguese (pt-BR).
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

MAX_BYTES = 50 * 1024 * 1024
TIMEOUT_S = 150
# One heavy conversion at a time per container; the Worker spreads load over several containers.
SLOT = asyncio.Semaphore(1)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class UserFacing(HTTPException):
    def __init__(self, status: int, msg: str):
        super().__init__(status_code=status, detail=msg)


# ---------- input checks ----------

def kind_of(data: bytes) -> str:
    if data[:5] == b"%PDF-" or b"%PDF-" in data[:1024]:
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "zip"  # docx/xlsx/pptx/odt/epub
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"  # doc/xls/ppt
    return "unknown"


def zip_kind(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "mimetype" in names and z.read("mimetype").strip() == b"application/epub+zip":
                return "epub"
            if any(n.startswith("word/") for n in names):
                return "docx"
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            if any(n.startswith("ppt/") for n in names):
                return "pptx"
            if "mimetype" in names and b"opendocument" in z.read("mimetype"):
                return "odf"
    except zipfile.BadZipFile:
        pass
    return "unknown"


def safe_stem(name: str) -> str:
    stem = Path(name or "arquivo").stem
    stem = re.sub(r"[^\w\- ]+", "", stem, flags=re.UNICODE).strip().replace(" ", "-")
    return (stem or "arquivo")[:80]


def content_disposition(name: str) -> str:
    """RFC 6266: ASCII fallback + UTF-8 filename* so names like "Relatório.pdf" survive."""
    import unicodedata
    from urllib.parse import quote

    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode() or "arquivo"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def run(cmd: list[str], cwd: Path) -> None:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise UserFacing(504, "A conversão demorou demais. Tente um arquivo menor.")
    if p.returncode != 0:
        # Ghostscript reports most errors on stdout, so log the tail of both streams.
        print(f"[cmd failed] {cmd[0]} rc={p.returncode} out={p.stdout[-600:]!r} err={p.stderr[-600:]!r}", flush=True)
        raise UserFacing(422, "Não foi possível converter este arquivo. Ele pode estar corrompido ou protegido por senha.")


def lo_convert(src: Path, fmt: str, work: Path) -> Path:
    profile = work / "lo-profile"
    run([
        "soffice", f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
        "--nolockcheck", "--convert-to", fmt, "--outdir", str(work / "out"), str(src),
    ], work)
    out = list((work / "out").glob("*"))
    if not out:
        raise UserFacing(422, "Não foi possível converter este arquivo.")
    return out[0]


def is_encrypted_pdf(src: Path) -> bool:
    p = subprocess.run(["pdfinfo", str(src)], capture_output=True, timeout=30)
    return b"Encrypted:      yes" in p.stdout or b"Incorrect password" in p.stderr


# ---------- converters ----------
# Each returns (bytes, filename, media_type).

def office_to_pdf(src: Path, work: Path, stem: str, opts: dict):
    out = lo_convert(src, "pdf", work)
    return out.read_bytes(), f"{stem}.pdf", "application/pdf"


def pdf_to_pdfa(src: Path, work: Path, stem: str, opts: dict):
    out = work / "out.pdf"
    run([
        "gs", "-dPDFA=2", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE", "-dSAFER", "--permit-file-read=/opt/",
        "-sColorConversionStrategy=RGB", "-sProcessColorModel=DeviceRGB",
        "-dPDFACompatibilityPolicy=1", "-sDEVICE=pdfwrite",
        f"-sOutputFile={out}", "/opt/PDFA_def.ps", str(src),
    ], work)
    return out.read_bytes(), f"{stem}-pdfa.pdf", "application/pdf"


# level → (color/gray image DPI, mono image DPI, JPEG QFactor: lower = better quality)
# Explicit settings instead of -dPDFSETTINGS presets: the /printer preset keeps 300 DPI and
# often re-encodes scanned pages *larger* than the original, and /screen barely beat /ebook.
COMPRESS = {
    "leve": (220, 600, 0.15),
    "recomendado": (150, 300, 0.40),
    "forte": (100, 300, 0.76),
}


def compress(src: Path, work: Path, stem: str, opts: dict):
    dpi, mono, q = COMPRESS.get(opts.get("level", "recomendado"), COMPRESS["recomendado"])
    out = work / "out.pdf"
    jpeg = f"<< /QFactor {q} /Blend 1 /HSamples [2 1 1 2] /VSamples [2 1 1 2] >>"
    run([
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.6",
        "-dNOPAUSE", "-dQUIET", "-dBATCH", "-dSAFER",
        "-dDetectDuplicateImages=true", "-dCompressFonts=true", "-dSubsetFonts=true",
        "-dDownsampleColorImages=true", "-dDownsampleGrayImages=true", "-dDownsampleMonoImages=true",
        "-dColorImageDownsampleType=/Bicubic", "-dGrayImageDownsampleType=/Bicubic",
        "-dMonoImageDownsampleType=/Subsample",
        f"-dColorImageResolution={dpi}", f"-dGrayImageResolution={dpi}", f"-dMonoImageResolution={mono}",
        "-dColorImageDownsampleThreshold=1.0", "-dGrayImageDownsampleThreshold=1.0",
        "-dMonoImageDownsampleThreshold=1.0",
        "-dAutoFilterColorImages=false", "-dAutoFilterGrayImages=false",
        "-dColorImageFilter=/DCTEncode", "-dGrayImageFilter=/DCTEncode",
        f"-sOutputFile={out}",
        "-c", f"<< /ColorImageDict {jpeg} /GrayImageDict {jpeg} >> setdistillerparams",
        "-f", str(src),
    ], work)
    data = out.read_bytes()
    if len(data) >= src.stat().st_size:
        raise UserFacing(422, "Este PDF já está otimizado: não foi possível reduzir mais o tamanho.")
    return data, f"{stem}-comprimido.pdf", "application/pdf"


MAX_WORD_PAGES = 200


def pdf_to_word(src: Path, work: Path, stem: str, opts: dict):
    import fitz  # PyMuPDF
    from pdf2docx import Converter

    with fitz.open(src) as doc:
        if doc.page_count > MAX_WORD_PAGES:
            raise UserFacing(413, f"O PDF tem páginas demais (máximo de {MAX_WORD_PAGES}).")
        has_text = any(page.get_text("text").strip() for page in doc)
    if not has_text:
        raise UserFacing(422, "Este PDF parece ser escaneado (só imagens). A conversão para Word com OCR estará disponível em breve.")
    out = work / "out.docx"
    cv = Converter(str(src))
    try:
        cv.convert(str(out), multi_processing=False)
    except Exception as e:  # pdf2docx raises generic exceptions on odd layouts
        print(f"[pdf2docx] {e!r}", flush=True)
        raise UserFacing(422, "Não foi possível converter este PDF para Word.")
    finally:
        cv.close()
    return out.read_bytes(), f"{stem}.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def pdf_to_excel(src: Path, work: Path, stem: str, opts: dict):
    import pdfplumber
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    found = 0
    with pdfplumber.open(src) as pdf:
        if len(pdf.pages) > 300:
            raise UserFacing(413, "O PDF tem páginas demais (máximo de 300).")
        for i, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()
            for t_idx, table in enumerate(tables, start=1):
                found += 1
                ws = wb.create_sheet(f"P{i}-T{t_idx}"[:31])
                for row in table:
                    ws.append([(c or "").strip() for c in row])
    if not found:
        # No ruled tables: fall back to text lines, one per row, so the user still gets data.
        ws = wb.create_sheet("Texto")
        with pdfplumber.open(src) as pdf:
            for page in pdf.pages:
                # layout=True keeps column spacing, so 2+ spaces mark a cell boundary.
                for line in (page.extract_text(layout=True) or "").splitlines():
                    if line.strip():
                        ws.append(re.split(r"\s{2,}", line.strip()))
    path = work / "out.xlsx"
    wb.save(path)
    return path.read_bytes(), f"{stem}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def pdf_to_pptx(src: Path, work: Path, stem: str, opts: dict):
    from pptx import Presentation
    from pptx.util import Emu

    imgs = work / "img"
    imgs.mkdir()
    run(["pdftoppm", "-r", "150", "-png", "-l", "200", str(src), str(imgs / "p")], work)
    pages = sorted(imgs.glob("p*.png"))
    if not pages:
        raise UserFacing(422, "Não foi possível ler as páginas deste PDF.")
    from PIL import Image  # installed with python-pptx

    w, h = Image.open(pages[0]).size
    prs = Presentation()
    prs.slide_width = Emu(9144000)  # 10 in
    prs.slide_height = Emu(int(9144000 * h / w))
    blank = prs.slide_layouts[6]
    for p in pages:
        s = prs.slides.add_slide(blank)
        s.shapes.add_picture(str(p), 0, 0, width=prs.slide_width, height=prs.slide_height)
    path = work / "out.pptx"
    prs.save(path)
    return path.read_bytes(), f"{stem}.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def ebook(target: str):
    def conv(src: Path, work: Path, stem: str, opts: dict):
        out = work / f"out.{target}"
        run(["ebook-convert", str(src), str(out)], work)
        media = "application/epub+zip" if target == "epub" else "application/x-mobipocket-ebook"
        return out.read_bytes(), f"{stem}.{target}", media
    return conv


def pdf_to_xml(src: Path, work: Path, stem: str, opts: dict):
    run(["pdftohtml", "-xml", "-i", "-q", "-nodrm", str(src), str(work / "out")], work)
    out = work / "out.xml"
    if not out.exists():
        raise UserFacing(422, "Não foi possível extrair dados deste PDF.")
    return out.read_bytes(), f"{stem}.xml", "application/xml"


# tool slug → (accepted input kinds, converter)
TOOLS = {
    "word-para-pdf": ({"docx", "ole", "odf"}, office_to_pdf),
    "excel-para-pdf": ({"xlsx", "ole", "odf"}, office_to_pdf),
    "powerpoint-para-pdf": ({"pptx", "ole", "odf"}, office_to_pdf),
    "pdf-para-pdfa": ({"pdf"}, pdf_to_pdfa),
    "comprimir-pdf": ({"pdf"}, compress),
    "pdf-para-excel": ({"pdf"}, pdf_to_excel),
    "pdf-para-powerpoint": ({"pdf"}, pdf_to_pptx),
    "pdf-para-epub": ({"pdf"}, ebook("epub")),
    "pdf-para-mobi": ({"pdf"}, ebook("mobi")),
    "epub-para-mobi": ({"epub"}, ebook("mobi")),
    "pdf-para-xml": ({"pdf"}, pdf_to_xml),
    "pdf-para-word": ({"pdf"}, pdf_to_word),
}

EXT = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "epub": ".epub", "odf": ".odt", "ole": ".doc"}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/convert/{tool}")
async def convert(tool: str, file: UploadFile = File(...), level: str = Form("recomendado")):
    if tool not in TOOLS:
        raise UserFacing(404, "Ferramenta indisponível.")
    accepted, fn = TOOLS[tool]
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise UserFacing(413, "Arquivo muito grande (máximo de 50 MB).")
    if not data:
        raise UserFacing(400, "O arquivo está vazio.")

    async with SLOT:
        work = Path(tempfile.mkdtemp(prefix="job-"))
        try:
            (work / "out").mkdir()
            k = kind_of(data)
            src = work / f"input{EXT.get(k, '.bin')}"
            src.write_bytes(data)
            del data
            if k == "zip":
                k = zip_kind(src)
                src = src.rename(work / f"input{EXT.get(k, '.zip')}")
            if k not in accepted:
                raise UserFacing(415, "Formato de arquivo não aceito por esta ferramenta.")
            if k == "pdf" and is_encrypted_pdf(src):
                raise UserFacing(422, "Este PDF está protegido por senha. Remova a senha e tente de novo.")
            body, name, media = await asyncio.to_thread(fn, src, work, safe_stem(file.filename), {"level": level})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return Response(
        content=body,
        media_type=media,
        headers={
            "Content-Disposition": content_disposition(name),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
