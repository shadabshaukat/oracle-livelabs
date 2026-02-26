#!/usr/bin/env bash
set -euo pipefail
BASE="http://127.0.0.1:8000"
COOKIE="/tmp/e2e_local_all.cookie"
EMAIL="e2e_local_all_$(date +%s)@example.com"
PASS="Password123!"
SAMPLE_DIR="/Users/shadab/Downloads/oracle-livelabs/search-app/sample"
OUT="/tmp/local_all_files_validation"
mkdir -p "$OUT"

curl -sS -o "$OUT/register.json" -w "%{http_code}" -c "$COOKIE" -X POST "$BASE/api/register" \
  -H "Content-Type: application/json" -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" > "$OUT/register.code" || true

python3 - <<'PY'
from pathlib import Path
import json
sample=Path('/Users/shadab/Downloads/oracle-livelabs/search-app/sample')
files=sorted([p.name for p in sample.iterdir() if p.is_file()])
Path('/tmp/local_all_files_validation/files.json').write_text(json.dumps(files,indent=2))
print('\n'.join(files))
PY

while IFS= read -r fname; do
  [ -z "$fname" ] && continue
  safe=$(python3 - <<PY
import re
s='''$fname'''
print(re.sub(r'[^A-Za-z0-9._-]+','_',s))
PY
)
  curl -sS --max-time 300 -o "$OUT/upload_${safe}.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/upload" \
    -F "files=@$SAMPLE_DIR/$fname" > "$OUT/upload_${safe}.code" || true
  echo "$fname -> $(cat "$OUT/upload_${safe}.code" 2>/dev/null || echo 'NA')"
done < <(python3 - <<'PY'
import json
for x in json.load(open('/tmp/local_all_files_validation/files.json')):
    print(x)
PY
)

curl -sS -o "$OUT/search.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/search" \
  -H "Content-Type: application/json" -d '{"query":"Mariam","k":5}' > "$OUT/search.code" || true
curl -sS -o "$OUT/image_search.json" -w "%{http_code}" -b "$COOKIE" -X POST "$BASE/api/image-search" -F "query=bonfire" > "$OUT/image_search.code" || true

python3 - <<'PY'
import json, glob, os, re
out='/tmp/local_all_files_validation'
results=[]
for code_path in sorted(glob.glob(f'{out}/upload_*.code')):
    key=os.path.basename(code_path)[7:-5]
    code=open(code_path).read().strip() if os.path.exists(code_path) else ''
    jpath=f'{out}/upload_{key}.json'
    payload={}
    try: payload=json.load(open(jpath))
    except Exception: payload={}
    item={'file_key':key,'upload_code':code,'document_id':None,'chunks':None,'status':None,'filename':None}
    try:
        r=payload.get('results',[{}])[0]
        item.update({'document_id':r.get('document_id'),'chunks':r.get('chunks'),'status':r.get('status'),'filename':r.get('filename')})
    except Exception:
        pass
    results.append(item)
json.dump(results,open(f'{out}/uploads_summary.json','w'),indent=2)
print(json.dumps(results,indent=2))
PY

python3 - <<'PY'
import json, os, requests
out='/tmp/local_all_files_validation'
base='http://127.0.0.1:8000'
cookie_file='/tmp/e2e_local_all.cookie'
cookies={}
# simple netscape cookie parse
for ln in open(cookie_file):
    if ln.startswith('#') or not ln.strip():
        continue
    parts=ln.strip().split('\t')
    if len(parts)>=7:
        cookies[parts[5]]=parts[6]
rows=json.load(open(f'{out}/uploads_summary.json'))
cycle=[]
for r in rows:
    did=r.get('document_id')
    if not did:
        cycle.append({'filename':r.get('filename') or r.get('file_key'),'document_id':None,'download':None,'delete':None,'download_after_delete':None})
        continue
    d1=requests.get(f'{base}/api/doc-download',params={'doc_id':did},cookies=cookies,timeout=30)
    d2=requests.delete(f'{base}/api/documents/{did}',cookies=cookies,timeout=30)
    d3=requests.get(f'{base}/api/doc-download',params={'doc_id':did},cookies=cookies,timeout=30)
    cycle.append({'filename':r.get('filename') or r.get('file_key'),'document_id':did,'download':d1.status_code,'delete':d2.status_code,'download_after_delete':d3.status_code})
json.dump(cycle,open(f'{out}/cycle_summary.json','w'),indent=2)
print(json.dumps(cycle,indent=2))
PY

python3 - <<'PY'
import json, os
out='/tmp/local_all_files_validation'
def code(name):
 p=f'{out}/{name}.code'
 return open(p).read().strip() if os.path.exists(p) else ''
uploads=json.load(open(f'{out}/uploads_summary.json'))
cycle=json.load(open(f'{out}/cycle_summary.json'))
final={
 'register':code('register'),
 'search_code':code('search'),
 'image_search_code':code('image_search'),
 'uploaded_total':len(uploads),
 'uploaded_ok':sum(1 for x in uploads if x.get('upload_code')=='200'),
 'upload_failures':[x for x in uploads if x.get('upload_code')!='200'],
 'docx_chunks':{x.get('filename') or x.get('file_key'):x.get('chunks') for x in uploads if (x.get('filename') or '').lower().endswith('.docx')},
 'pdf_chunks':{x.get('filename') or x.get('file_key'):x.get('chunks') for x in uploads if (x.get('filename') or '').lower().endswith('.pdf')},
 'full_cycle_failures':[x for x in cycle if x.get('download')!=200 or x.get('delete')!=200 or x.get('download_after_delete')!=404]
}
json.dump(final,open(f'{out}/final_summary.json','w'),indent=2)
print(json.dumps(final,indent=2))
PY
