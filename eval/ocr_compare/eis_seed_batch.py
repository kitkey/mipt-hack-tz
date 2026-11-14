from playwright.sync_api import sync_playwright
from pathlib import Path
import json,re,pymupdf,hashlib
out=Path('eval/ocr_compare/real_eis');out.mkdir(exist_ok=True)
numbers=['32616346750','32616554818','32616395175','32616314507','32616553079','32616306221','32616297021','32616378673','32616364734','32616441453','32616292704']
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
 page=b.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(3000)
 for num in numbers:
  try:
   page.goto('https://zakupki.gov.ru/epz/order/extendedsearch/results.html?fz223=on&searchString='+num,wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(3000)
   links=page.locator('a[href*="notice223/documents.html"]').evaluate_all('(els)=>els.map(x=>x.href)')
   if not links: print('NO_SEARCH',num,flush=True);continue
   url=links[0];page.goto(url,wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(3000)
   items=page.locator('a').evaluate_all('(els)=>els.map(x=>({text:(x.textContent||"").trim(),href:String(x.href||"")})).filter(x=>x.text.toLowerCase().includes("техническое задание")&&x.text.toLowerCase().includes(".pdf"))')
   if not items:print('NO_PDF',num,flush=True);continue
   x=items[0]; uid=re.search('uid=([^&]+)',x['href']).group(1); dest=out/(uid+'.pdf')
   with page.expect_download(timeout=60000) as d: page.locator('a').filter(has_text=x['text']).first.evaluate('(e)=>e.click()')
   d.value.save_as(str(dest)); pdf=pymupdf.open(dest); counts=[len(pg.get_text().strip()) for pg in pdf];pdf.close()
   record={'number':num,'purchase':url,'attachment':x,'page_chars':counts,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()}
   (out/(uid+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
   print('RESULT',num,uid,counts,flush=True);page.wait_for_timeout(3000)
  except Exception as e:print('ERROR',num,repr(e)[:300],flush=True)
 b.close()
