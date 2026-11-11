# conversordepdf.pro — conversion engine

Cloudflare Worker + Cloudflare Container that powers the server-side tools of
https://conversordepdf.pro (Word/Excel/PowerPoint → PDF, PDF → Word/Excel/PowerPoint,
PDF/A, compress, EPUB/MOBI, XML).

- `src/index.ts` — Worker: validation, rate limiting, routing to containers
- `container/app.py` — FastAPI conversion service (LibreOffice, Ghostscript, Calibre, poppler, pdfplumber, pdf2docx/PyMuPDF)
- `container/smoke-test.sh` — converts real files with every tool (run in CI before each deploy)

Uploaded files exist only in a temporary folder during the request and are deleted right after.

## License

GNU Affero General Public License v3.0 (see `LICENSE`), because the PDF → Word
converter uses PyMuPDF (AGPL-3.0). Third-party engines keep their own licenses.
