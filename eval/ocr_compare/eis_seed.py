from playwright.sync_api import sync_playwright
from pathlib import Path
import json,re,pymupdf,hashlib
out=Path('eval/ocr_compare/real_eis');out.mkdir(exist_ok=True)
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
 page=b.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(4000)
 page.goto('https://zakupki.gov.ru/epz/order/extendedsearch/results.html?fz223=on&searchString=32616329831',wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(4000)
 links=page.locator('a[href*="notice223/documents.html"]').evaluate_all('(els)=>els.map(x=>x.href)'); print('links',links,flush=True)
 for url in links[:1]:
  page.goto(url,wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(4000)
  items=page.locator('a').evaluate_all('(els)=>els.map(x=>({text:(x.textContent||"").trim(),href:String(x.href||"")})).filter(x=>x.text.toLowerCase().includes("техническое задание")&&x.text.toLowerCase().includes(".pdf"))')
  print('items',json.dumps(items,ensure_ascii=False),flush=True)
  for x in items[:2]:
   try:
    uid=re.search('uid=([^&]+)',x['href']).group(1); dest=out/(uid+'.pdf')
    with page.expect_download(timeout=60000) as d: page.locator('a').filter(has_text=x['text']).first.evaluate('(e)=>e.click()')
    d.value.save_as(str(dest));pdf=pymupdf.open(dest); counts=[len(pg.get_text().strip()) for pg in pdf]
    print('RESULT',uid,counts,flush=True)
    (out/(uid+'.json')).write_text(json.dumps({'purchase':url,'attachment':x,'page_chars':counts,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
    page.wait_for_timeout(4000)
   except Exception as e: print('ERROR',repr(e),flush=True)
 b.close()

