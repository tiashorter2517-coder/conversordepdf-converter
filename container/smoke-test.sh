#!/usr/bin/env bash
# Smoke test: builds real input files inside the image, then runs every converter
# through the HTTP API and checks the output type. Used by CI before each deploy.
set -euo pipefail
IMG="${1:-conversordepdf-converter}"
NAME=conv-test
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p 8080:8080 "$IMG" >/dev/null
trap 'docker logs "$NAME" | tail -40; docker rm -f "$NAME" >/dev/null' EXIT

for i in $(seq 1 60); do curl -fs localhost:8080/health >/dev/null && break; sleep 1; done

# Fixtures generated with the tools inside the image.
F=$(mktemp -d)
docker exec "$NAME" bash -c '
  set -e; cd /tmp; mkdir -p fx && cd fx
  printf "Relatório de teste\nLinha 2 com acentuação: ção, ã, é\n" > doc.txt
  printf "Nome,Valor\nA,1\nB,2\n" > sheet.csv
  soffice -env:UserInstallation=file:///tmp/lo-fx --headless --convert-to docx doc.txt >/dev/null
  soffice -env:UserInstallation=file:///tmp/lo-fx --headless --convert-to xlsx sheet.csv >/dev/null
  /opt/venv/bin/python -c "
from pptx import Presentation
p = Presentation(); s = p.slides.add_slide(p.slide_layouts[1])
s.shapes.title.text = \"Apresentação de teste\"; s.placeholders[1].text = \"Linha com acentuação\"
p.save(\"doc.pptx\")"
  soffice -env:UserInstallation=file:///tmp/lo-fx --headless --convert-to pdf doc.txt >/dev/null
  soffice -env:UserInstallation=file:///tmp/lo-fx --headless --convert-to pdf sheet.csv >/dev/null
  ebook-convert doc.txt book.epub >/dev/null
  ls -la'
for f in doc.docx sheet.xlsx doc.pptx doc.pdf sheet.pdf book.epub; do docker cp "$NAME:/tmp/fx/$f" "$F/$f"; done

fail=0
check() { # tool input expected-magic
  local tool=$1 in=$2 magic=$3 out="$F/out-$1"
  code=$(curl -s -o "$out" -w '%{http_code}' -F "file=@$F/$in" -F level=forte "localhost:8080/convert/$tool")
  if [ "$code" = 200 ] && head -c 100 "$out" | grep -qa "$magic"; then
    echo "✓ $tool ($(stat -c%s "$out") bytes)"
  elif [ "$tool" = comprimir-pdf ] && [ "$code" = 422 ]; then
    echo "✓ $tool (already optimal → 422 as designed)"
  else
    echo "✖ $tool → HTTP $code: $(head -c 300 "$out")"; fail=1
  fi
}
check word-para-pdf doc.docx '%PDF'
check excel-para-pdf sheet.xlsx '%PDF'
check powerpoint-para-pdf doc.pptx '%PDF'
check pdf-para-pdfa doc.pdf '%PDF'
check comprimir-pdf doc.pdf '%PDF'
check pdf-para-excel sheet.pdf 'PK'
check pdf-para-powerpoint doc.pdf 'PK'
check pdf-para-epub doc.pdf 'PK'
check pdf-para-mobi doc.pdf 'BOOKMOBI\|MOBI'
check epub-para-mobi book.epub 'BOOKMOBI\|MOBI'
check pdf-para-xml doc.pdf '<?xml'
check pdf-para-word doc.pdf 'PK'

# Rejections
code=$(curl -s -o /dev/null -w '%{http_code}' -F "file=@$F/doc.docx" localhost:8080/convert/pdf-para-xml)
[ "$code" = 415 ] && echo "✓ wrong type rejected (415)" || { echo "✖ wrong type → $code"; fail=1; }

exit $fail
