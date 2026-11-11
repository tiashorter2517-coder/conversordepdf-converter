# Conversion engine for conversordepdf.pro (runs on Cloudflare Containers).
# No internet access at runtime; files live only in /tmp for the duration of one request.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QT_QPA_PLATFORM=offscreen \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox" \
    HOME=/tmp

# LibreOffice (Office → PDF), Calibre (e-books), Ghostscript (compress, PDF/A),
# poppler-utils (PDF → images, PDF → XML), plus metric-compatible fonts so Word/Excel
# layouts don't shift (Carlito = Calibri, Caladea = Cambria, Liberation = Arial/Times/Courier).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
      calibre ghostscript poppler-utils \
      python3 python3-venv libglib2.0-0 libgomp1 \
      fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core fonts-noto-core \
      ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# PDF/A definition file + a copy of Ghostscript's sRGB ICC profile in /opt
# (Ghostscript's SAFER mode only reads it because app.py passes --permit-file-read=/opt/).
RUN ICC=$(find /usr/share/ghostscript /usr/share/color -name 'default_rgb.icc' -o -name 'srgb.icc' 2>/dev/null | head -1) \
  && test -n "$ICC" && cp "$ICC" /opt/srgb.icc && ICC=/opt/srgb.icc \
  && printf '%%!\n/ICCProfile (%s) def\n[/_objdef {icc_PDFA} /type /stream /OBJ pdfmark\n[{icc_PDFA} <</N 3>> /PUT pdfmark\n[{icc_PDFA} ICCProfile (r) file /PUT pdfmark\n[/_objdef {OutputIntent_PDFA} /type /dict /OBJ pdfmark\n[{OutputIntent_PDFA} <<\n  /Type /OutputIntent /S /GTS_PDFA1\n  /DestOutputProfile {icc_PDFA}\n  /OutputConditionIdentifier (sRGB)\n>> /PUT pdfmark\n[{Catalog} <</OutputIntents [ {OutputIntent_PDFA} ]>> /PUT pdfmark\n' "$ICC" > /opt/PDFA_def.ps

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --home-dir /tmp conv
USER conv

EXPOSE 8080
CMD ["/opt/venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
