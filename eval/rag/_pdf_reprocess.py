import json, concurrent.futures
from pathlib import Path
from tzcheck.rag.collect_inventory import fetch
old=json.loads(Path('eval/rag/collection_results.json').read_text(encoding='utf8'))
items=[{'query':r['designation'],'results':[{'url':r['source']}]} for r in old if r.get('kind')=='pdf']
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as p:
 result=[]
 for r in p.map(fetch,items):
  result.append(r);print(r['designation'],r['status'],flush=True)
Path('eval/rag/pdf_reprocess.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
