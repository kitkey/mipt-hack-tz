"""Collect discoverable public copies, requiring an exact designation match."""
import concurrent.futures, json, re, hashlib
from pathlib import Path
from datetime import date
from urllib.parse import urljoin
import requests
import pymupdf
from lxml import html

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'data/norm_corpus'
SEARCH=ROOT/'eval/rag/search_results.json'

def normalized(s):
 return re.sub(r'\s+', ' ', s.upper().replace('–','-').replace('—','-').replace('\xa0',' ')).strip()
def matches(needle,text):
 # Do not confuse GOST with GOST R or a different revision.
 n=normalized(needle); t=normalized(text)
 return bool(re.search(r'(?<![А-ЯA-Z\d])'+re.escape(n).replace(r'\ ',r'\s+')+r'(?!\d)',t))

def parse_html(raw):
 tree=html.fromstring(raw)
 for x in tree.xpath('//script|//style|//nav|//footer'): x.drop_tree()
 mega=tree.xpath('//div[@class="contener_doc"]'); word=tree.xpath('//div[contains(@class,"WordSection")]')
 body=(mega or word or tree.xpath('//body'))[0]
 els=body.xpath('./div[starts-with(@class,"s")] | ./table') if mega else body.xpath('.//p[not(ancestor::table)] | .//h1[not(ancestor::table)] | .//h2[not(ancestor::table)] | .//h3[not(ancestor::table)] | .//table[not(ancestor::table)]')
 out=[]
 for e in els:
  if e.tag=='table':
   value='\n'.join(' | '.join(' '.join(c.text_content().split()) for c in row.xpath('./th|./td')) for row in e.xpath('.//tr'))
  else: value=' '.join(e.text_content().split())
  if value.strip(' |\n'): out.append({'type':'table' if e.tag=='table' else 'text','text':value})
 return out

def parse_pdf(raw):
 """Текстовый PDF -> (блоки текста и таблиц в порядке страницы, текст страниц)."""
 doc=pymupdf.open(stream=raw,filetype='pdf'); pages=[p.get_text(sort=True) for p in doc]; blocks=[]
 for page in doc:
  try: tables=list(page.find_tables().tables)
  except Exception: tables=[]
  regions=[]
  for table in tables:
   table_text='\n'.join(' | '.join((cell or '').strip() for cell in row) for row in table.extract())
   if table_text.strip(' |\n'): regions.append((table.bbox[1],{'type':'table','text':table_text}))
  for block in page.get_text('blocks',sort=True):
   rect=pymupdf.Rect(block[:4]); center=(rect.x0+rect.width/2,rect.y0+rect.height/2)
   if any(pymupdf.Rect(t.bbox).contains(center) for t in tables): continue
   for line in block[4].splitlines():
    line=' '.join(line.split())
    if line: regions.append((rect.y0,{'type':'text','text':line}))
  blocks.extend(x[1] for x in sorted(regions,key=lambda x:x[0]))
 return blocks,pages

def fetch(item):
 title=item['query']; candidates=item['results'][:3]; failures=[]
 if title in ('ГОСТ 53782-2010','ГОСТ 53783-2010'): return {'designation':title,'status':'ambiguous','reason':'Р-версия не тождественна указанному ГОСТ'}
 for candidate in candidates:
  try:
   url=candidate['url']; r=requests.get(url,timeout=40,headers={'User-Agent':'Mozilla/5.0'}); r.raise_for_status()
   if '/Index' in url or '/index' in url:
    tree=html.fromstring(r.content)
    links=[urljoin(url,x) for x in tree.xpath('//a/@href') if re.search(r'/Data\d*/.*\.(?:htm|html|pdf)$',x,re.I)]
    if not links: raise ValueError('карточка без полного текста')
    url=links[0]; r=requests.get(url,timeout=40,headers={'User-Agent':'Mozilla/5.0'}); r.raise_for_status()
   if r.content.startswith(b'%PDF'):
    blocks,pages=parse_pdf(r.content); text='\n'.join(pages); head=' '.join(pages[:3])
    kind='pdf'; totalpages=len(pages)
   else:
    blocks=parse_html(r.content); text='\n'.join(b['text'] for b in blocks); head=text[:12000]; kind='html'; totalpages=None
   if not matches(title,head): raise ValueError('обозначение не подтверждено в начале документа')
   if len(text)<2000: raise ValueError('недостаточно текста, возможно скан или карточка')
   first=normalized(head[:1500])
   compact=re.sub(r'\s+','',first)
   if title.upper().startswith('СНИП') and 'АКТУАЛИЗИРОВАННАЯРЕДАКЦИЯ' in compact and re.search(r'СП\d',compact):
    raise ValueError('это актуализированный СП, а не исходный СНиП')
   if kind=='pdf' and totalpages==1:
    raise ValueError('одна страница: вероятно письмо или карточка, а не полный документ')
   name='src-'+hashlib.sha256(title.encode()).hexdigest()[:16]
   if kind=='pdf':
    (OUT/'raw').mkdir(exist_ok=True)
    (OUT/'raw'/(name+'.pdf')).write_bytes(r.content)
   year=re.search(r'(?:-|\.)(\d{2,4})$',title)
   data={'id':name,'document':title,'edition':'редакция источника не подтверждена','edition_date':None,'status':'не проверен','source':url,'retrieved':date.today().isoformat(),'blocks':blocks,'extraction':kind,'pages':totalpages}
   (OUT/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
   return {'designation':title,'status':'collected','source':url,'kind':kind,'characters':len(text),'pages':totalpages}
  except Exception as e: failures.append(type(e).__name__+': '+str(e)[:100])
 return {'designation':title,'status':'unavailable','reason':'; '.join(failures) or 'поиск не нашёл открытой копии'}

def main():
 items=json.loads(SEARCH.read_text(encoding='utf8'))
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  output=[]
  for x in pool.map(fetch,items):
   output.append(x); print(x['designation'],x['status'],flush=True)
 (ROOT/'eval/rag/collection_results.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf8')
if __name__=='__main__': main()
