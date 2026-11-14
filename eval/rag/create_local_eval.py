"""Deterministic local evaluation: real TZ references, synthetic titles and out-of-corpus references."""
import hashlib,json,random,re,sys,zipfile
from pathlib import Path
from lxml import etree
ROOT=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(ROOT))
from tzcheck.pii import Scrubber
from tzcheck.rag import Search

def document_text(p):
 if p.suffix.lower()=='.txt': return p.read_text(encoding='utf8',errors='ignore')
 if p.suffix.lower()=='.docx':
  with zipfile.ZipFile(p) as z: return '\n'.join(' '.join(x.itertext()) for x in etree.fromstring(z.read('word/document.xml')).xpath('//*[local-name()="p"]'))
 if p.suffix.lower()=='.pdf':
  import pymupdf
  with pymupdf.open(p) as doc: return '\n'.join(page.get_text(sort=True) for page in doc)
 if p.suffix.lower()=='.xlsx':
  import openpyxl
  book=openpyxl.load_workbook(p,read_only=True,data_only=True)
  try: return '\n'.join(' '.join(str(c) for c in row if c is not None) for sheet in book for row in sheet.iter_rows(values_only=True))
  finally: book.close()
 return ''
def key(s): return re.sub(r'\s+',' ',s.upper().replace('—','-').replace('–','-')).strip(' .')

def main():
 search=Search(); rows=search.rows; random.Random(42).shuffle(rows)
 docs={key(r['document']) for r in rows}; positives=[]; refs=[]; seen=set(); roots=[ROOT/'samples',ROOT/'data_raw/eis_more',ROOT/'eval/rag/eis_documents']
 # Support both "п. 4.1 ГОСТ ..." and "ГОСТ ... п. 4.1" inside one TZ paragraph.
 standard=r'(?:ГОСТ(?:\s+Р)?(?:\s+(?:ISO|ИСО|ЕН))?|СП|СНиП)\s+\d[\d.]*\s*[-–—]\s*\d{2,4}'
 clause=r'(?:пункт(?:а|у|е)?|п\.)\s*(\d+(?:\.\d+)*)'
 rx=re.compile('('+standard+').{0,100}?'+clause+'|'+clause+'.{0,100}?('+standard+')',re.I)
 scrub=Scrubber()
 for root in roots:
  if not root.exists(): continue
  for p in root.rglob('*'):
   if p.suffix.lower() not in ('.docx','.txt','.pdf','.xlsx'): continue
   try: text=document_text(p)
   except Exception: continue
   for line in text.splitlines():
    for m in rx.finditer(line):
     d=key(m.group(1) or m.group(4)); n=m.group(2) or m.group(3)
     target=[x for x in rows if key(x['document'])==d and x['number']==n and x['kind']=='clause']
     q=scrub.scrub(' '.join(line.split())[:500]); unique=(d,n,q)
     if not target or unique in seen or len(q)<25: continue
     seen.add(unique); refs.append({'question':q,'document':d,'number':n,'origin':'real_reference','source_file':p.name,'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
 for r in rows:
  title=' '.join(r['title'].split()); number=r['number']
  if r['kind']!='clause' or len(title)<15 or len(title)>95 or len(r['text'])<100: continue
  if re.search(r'долж|следует|необходимо|обязан|допускается|требу|допуск',title,re.I): continue
  if len(title.split())<3 or any(c in title for c in '|;'): continue
  k=(r['document'],number)
  if k in seen: continue
  seen.add(k); positives.append({'question':'Какие требования установлены по теме: '+title.rstrip('.:')+'?', 'document':r['document'],'number':number,'origin':'synthetic_title'})
  if len(positives)>=200: break
 missing=['ГОСТ Р 59044-2026','ГОСТ Р 53780-2010','ГОСТ Р 55969-2023','СП 2.2.4285-26']
 missing=[d for d in missing if key(d) not in docs]
 negatives=[{'question':f'Какие требования содержит пункт {i}.1 {d}?','document':d,'number':f'{i}.1','origin':'absent_standard'} for d in missing for i in range(1,6)]
 negatives += [{'question':s,'origin':'out_of_domain'} for s in ['Как оборудовать марсианскую колонию для выращивания картофеля?','Какие правила лицензирования межзвездного телепорта?','Какая масса спутника Юпитера Европа?','Какой рецепт торта с малиной и шоколадом?','Какой двигатель использовать для полёта к Альфе Центавра?']]
 result=refs+positives+negatives
 (ROOT/'eval/rag/questions_local.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
 print('real',len(refs),'synthetic',len(positives),'negative',len(negatives))
if __name__=='__main__': main()
