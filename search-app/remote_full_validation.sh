#!/usr/bin/env bash
set -euo pipefail
BASE="http://127.0.0.1:8000"
COOKIE="/tmp/e2e_remote_full.cookie"
EMAIL="e2e_remote_full_$(date +%s)@example.com"
PASS="Password123!"
OUT="/tmp/remote_full_validation"
mkdir -p "$OUT"

curl -sS -o "$OUT/register.json" -w "%{http_code}" -c "$COOKIE" -X POST "$BASE/api/register" -H "Content-Type: application/json" -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" > "$OUT/register.code" || true

for ep in /api/health /api/ready /api/kb /api/search-history; do
  name=$(echo "$ep" | tr '/' '_' | sed 's/^_//')
  curl -sS -o "$OUT/${name}.json" -w "%{http_code}" -b "$COOKIE" "$BASE$ep" > "$OUT/${name}.code" || true
done

curl -sS --max-time 180 -o "$OUT/upload_pdf.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/upload" -F "files=@/home/opc/oracle-livelabs/search-app/sample/Mariam.pdf" > "$OUT/upload_pdf.code" || true
curl -sS --max-time 120 -o "$OUT/upload_img.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/upload" -F "files=@/home/opc/oracle-livelabs/search-app/sample/Large_bonfire.jpg" > "$OUT/upload_img.code" || true
curl -sS --max-time 240 -o "$OUT/upload_docx.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/upload" -F "files=@/home/opc/oracle-livelabs/search-app/sample/Mariam.docx" > "$OUT/upload_docx.code" || true

python3 - <<'PY2'
import json
out='/tmp/remote_full_validation'

def j(p):
    try:
        return json.load(open(p))
    except Exception:
        return {}
pdf=j(f'{out}/upload_pdf.json'); img=j(f'{out}/upload_img.json'); docx=j(f'{out}/upload_docx.json')

def did(o):
    try: return o['results'][0]['document_id']
    except Exception: return None
ids={'pdf':did(pdf),'img':did(img),'docx':did(docx)}
json.dump(ids, open(f'{out}/ids.json','w'))
print(json.dumps(ids))
PY2

PDF_ID=$(python3 -c "import json; d=json.load(open('/tmp/remote_full_validation/ids.json')); print(d.get('pdf') or '')")
DOCX_ID=$(python3 -c "import json; d=json.load(open('/tmp/remote_full_validation/ids.json')); print(d.get('docx') or '')")

if [ -n "$DOCX_ID" ]; then
  curl -sS -o "$OUT/docx_chunks_preview.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/chunks-preview" -H "Content-Type: application/json" -d "{\"doc_id\":$DOCX_ID,\"limit\":3}" > "$OUT/docx_chunks_preview.code" || true
fi

curl -sS -o "$OUT/search.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/search" -H "Content-Type: application/json" -d '{"query":"Mariam","k":3}' > "$OUT/search.code" || true
curl -sS -o "$OUT/image_search.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/image-search" -F "query=bonfire" > "$OUT/image_search.code" || true

if [ -n "$PDF_ID" ]; then
  curl -sS -o "$OUT/doc_download_pdf.bin" -w "%{http_code}" -b "$COOKIE" "$BASE/api/doc-download?doc_id=$PDF_ID" > "$OUT/doc_download_pdf.code" || true
  curl -sS -o "$OUT/delete_pdf.json" -w "%{http_code}" -b "$COOKIE" -X DELETE "$BASE/api/documents/$PDF_ID" > "$OUT/delete_pdf.code" || true
  curl -sS -o "$OUT/doc_download_pdf_after_delete.bin" -w "%{http_code}" -b "$COOKIE" "$BASE/api/doc-download?doc_id=$PDF_ID" > "$OUT/doc_download_pdf_after_delete.code" || true
fi

SID=$(python3 - <<'PY3'
import json
try:
  d=json.load(open('/tmp/remote_full_validation/api_search-history.json'))
  s=d.get('sessions') or []
  print(s[0]['session_id'] if s else '')
except Exception:
  print('')
PY3
)
if [ -n "$SID" ]; then
  curl -sS -o "$OUT/search_history_detail.json" -w "%{http_code}" -b "$COOKIE" "$BASE/api/search-history/$SID?limit=20&offset=0" > "$OUT/search_history_detail.code" || true
fi

python3 - <<'PY4'
import json, os
out='/tmp/remote_full_validation'
def c(n):
  p=f'{out}/{n}.code'; return open(p).read().strip() if os.path.exists(p) else ''
def j(n):
  p=f'{out}/{n}.json'
  if not os.path.exists(p): return {}
  try: return json.load(open(p))
  except Exception: return {}
summary={
 'register':c('register'),'health':c('api_health'),'ready':c('api_ready'),'kb':c('api_kb'),'search_history':c('api_search-history'),
 'upload_pdf':c('upload_pdf'),'upload_img':c('upload_img'),'upload_docx':c('upload_docx'),'chunks_preview':c('docx_chunks_preview'),
 'search':c('search'),'image_search':c('image_search'),'doc_download_pdf':c('doc_download_pdf'),'delete_pdf':c('delete_pdf'),'doc_download_pdf_after_delete':c('doc_download_pdf_after_delete'),'search_history_detail':c('search_history_detail'),
 'docx_chunks':(j('upload_docx').get('results') or [{}])[0].get('chunks'),
 'docx_preview_head':((j('docx_chunks_preview').get('chunks') or [{}])[0].get('content') or '')[:220],
 'search_top_head':((j('search').get('hits') or [{}])[0].get('content') or '')[:220],
 'image_hits':len(j('image_search').get('hits') or [])
}
json.dump(summary, open(f'{out}/summary.json','w'), indent=2)
print(json.dumps(summary, indent=2))
PY4
