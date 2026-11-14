"""Collect PDF attachments from public 223-FZ search; keep source provenance."""
import json,re,time,hashlib
from pathlib import Path
from urllib.parse import quote
import pymupdf
from playwright.sync_api import sync_playwright
out=Path('eval/ocr_compare/real_eis');out.mkdir(exist_ok=True)
records=[]
with sync_playwright() as p:
 browser=p.chromium.launch(headless=False,executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
 page=browser.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(3000)
 for page_no in range(1,21):
  url='https://zakupki.gov.ru/epz/order/extendedsearch/results.html?fz223=on&searchString='+quote('техническое задание')+'&pageNumber='+str(page_no)
  try:
   page.goto(url,wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(3000)
   docs=page.locator('a[href*="notice223/documents.html"]').evaluate_all('(els)=>[...new Set(els.map(x=>x.href))]')
  except Exception as e: print('SEARCH_ERROR',page_no,str(e)[:100],flush=True);continue
  print('SEARCH',page_no,len(docs),flush=True)
  for docurl in docs:
   try:
    page.goto(docurl,wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(3000)
    items=page.locator('a[href*="/download/fz223/file.html"]').evaluate_all('(els)=>els.map(x=>({text:(x.textContent||"").trim(),href:String(x.href||"")}))')
    candidates=[x for x in items if '.pdf' in x['text'].lower() and re.search(r'технич|задани|тз\b|описание предмет|спецификац',x['text'],re.I)]
    for x in candidates[:3]:
     uid=re.search(r'uid=([^&]+)',x['href']).group(1)
     if any(r['uid']==uid for r in records): continue
     dest=out/(uid+'.pdf')
     try:
      loc=page.locator('a').filter(has_text=x['text']).first
      with page.expect_download(timeout=45000) as d: loc.click()
      d.value.save_as(str(dest));page.wait_for_timeout(3000)
      with pymupdf.open(dest) as pdf:
       counts=[len(pg.get_text().strip()) for pg in pdf]
       rec={'uid':uid,'name':x['text'],'purchase':docurl,'file_url':x['href'],'pages':len(counts),'page_chars':counts,'scan_pages':sum(n<20 for n in counts),'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()}
       records.append(rec);print('PDF',json.dumps(rec,ensure_ascii=False),flush=True)
       (out/'manifest.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
     except Exception as e: print('DOWNLOAD_ERROR',x['text'],str(e)[:120],flush=True)
   except Exception as e: print('DOC_ERROR',docurl,str(e)[:100],flush=True)
   if sum(r['scan_pages']==r['pages'] and r['pages']>0 for r in records)>=10:break
  if sum(r['scan_pages']==r['pages'] and r['pages']>0 for r in records)>=10:break
 browser.close()
