#!/usr/bin/env bash
# Download the open-licensed SOTIF source documents in sotif/sources.json and convert them to text for
# `scripts/build_ontology.sh --docs data/local/sotif-sources`. Texts stay local; the list (with licenses) is versioned.
# Only openly licensed documents (CC BY) are listed, so extracted facts and quoted evidence can ship with attribution.
set -euo pipefail
cd "$(dirname "$0")/.."  # the sample folder (works as a repository root or as a subfolder)
OUT=${1:-data/local/sotif-sources}
mkdir -p "$OUT/pdf" build
[ -x build/pdfvenv/bin/python ] && build/pdfvenv/bin/python -c 'import fitz' 2>/dev/null || { python3 -m venv build/pdfvenv && build/pdfvenv/bin/pip install -q pymupdf==1.24.14; }
python3 - "$OUT" <<'PY'
import json, subprocess, sys
out = sys.argv[1]
for src in json.load(open("sotif/sources.json")):
    pdf = f"{out}/pdf/{src['id']}.pdf"
    subprocess.run(["curl", "-sSLf", "-o", pdf, src["url"]], check=True)
    print(f"{src['id']}: {src['license']} {src['title']}")
PY
# -I: do not import anything from the download directory.
build/pdfvenv/bin/python -I - "$OUT" <<'PY'
import json, sys
from pathlib import Path
import fitz  # PyMuPDF: better Korean text extraction than pypdf
out = Path(sys.argv[1])
for src in json.loads(Path("sotif/sources.json").read_text()):
    text = "\n".join(page.get_text() for page in fitz.open(out / "pdf" / f"{src['id']}.pdf"))
    header = f"Source: {src['title']} ({src['authors']}, {src['year']}), {src['url']}, license {src['license']}\n\n"
    (out / f"{src['id']}.txt").write_text(header + text)
    print(f"{src['id']}.txt {len(text)} chars")
PY
