import sys,time,json,re,collections
from pathlib import Path
sys.path.insert(0,'.');sys.path.insert(0,'eval/ocr_compare')
import pdfplumber
from tzcheck.parse import parse
from benchmark_text import PAIRS,norm,distance,tokens,multirecall
out=[]
for name,p in PAIRS:
 ref=parse('samples/'+name+'.docx');start=time.perf_counter()
 with pdfplumber.open(p) as f:
  text='\n'.join(pg.extract_text() or '' for pg in f.pages)
  tables=[table for pg in f.pages for table in pg.extract_tables()]
  pages=len(f.pages)
 seconds=time.perf_counter()-start
 rt=norm(ref.text()); cand=norm(text);rw=tokens(rt);cw=tokens(cand)
 refcells=[norm(c) for b in ref.blocks if b.kind=='row' for c in b.cells if norm(c)]
 cells=[norm(c) for t in tables for row in t for c in row if c and norm(c)]
 nums=lambda s:re.findall(r'(?<!\w)\d+(?:[.,]\d+)*(?!\w)',s)
 codes=lambda s:re.findall(r'(?<!\d)\d{2}\.\d{2}\.\d{2}\.\d{3}(?!\d)',s)
 out.append({'name':name,'pages':pages,'seconds':round(seconds,3),'cer':distance(rt,cand)/len(rt),'wer':distance(rw,cw)/len(rw),'cells_exact_multiset':multirecall(refcells,cells),'numbers_exact_multiset':multirecall(nums(rt),nums(cand)),'okpd_exact_multiset':multirecall(codes(rt),codes(cand))})
 print(name,flush=True)
Path('eval/ocr_compare/results_pdfplumber.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
