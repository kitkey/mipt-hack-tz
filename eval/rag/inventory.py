"""List referenced designations locally; documents are never transmitted."""
import json,re,zipfile
from pathlib import Path
from lxml import etree
ROOT=Path(__file__).resolve().parents[2]
PAT=re.compile(r'\b(?:ГОСТ(?:\s+Р)?|СП|СНиП)\s*(?:[A-ZА-ЯЁ]+\s*)?\d[\d.\-–—]*(?:\s*[-–—]\s*\d{2,4})?',re.I)
def main():
 paths=[ROOT/'samples',ROOT/'data_raw/eis_more']; found={}; files=0
 for root in paths:
  if not root.exists(): continue
  for p in root.rglob('*'):
   if not p.is_file(): continue
   try:
    if p.suffix.lower()=='.docx':
     with zipfile.ZipFile(p) as z: t=' '.join(etree.fromstring(z.read('word/document.xml')).itertext())
    elif p.suffix.lower()=='.pdf':
     import fitz
     with fitz.open(p) as d: t=' '.join(page.get_text() for page in d)
    elif p.suffix.lower()=='.txt': t=p.read_text(encoding='utf8',errors='ignore')
    else: continue
    files+=1
    for m in PAT.findall(t): found.setdefault(' '.join(m.upper().split()),set()).add(str(p))
   except Exception: continue
 out={'files_read':files,'designations':{k:len(v) for k,v in sorted(found.items())}}
 (ROOT/'eval/rag/inventory.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8'); print('Документов:',files,'обозначений:',len(found))
if __name__=='__main__': main()
