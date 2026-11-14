from playwright.sync_api import sync_playwright
import json
from pathlib import Path
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
 page=b.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(3000)
 page.goto('https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=%D1%82%D0%B5%D1%85%D0%BD%D0%B8%D1%87%D0%B5%D1%81%D0%BA%D0%BE%D0%B5+%D0%B7%D0%B0%D0%B4%D0%B0%D0%BD%D0%B8%D0%B5&fz223=on',wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(4000)
 docs=page.locator('a[href*="notice223/documents.html"]').evaluate_all('(els)=>els.map(x=>x.href)')
 print('DOC PAGES',len(docs),flush=True)
 Path('eval/ocr_compare/eis_doc_pages.json').write_text(json.dumps(docs,ensure_ascii=False,indent=2),encoding='utf-8')
 for url in docs[:3]:
  page.goto(url,wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(4000)
  print('PAGE',page.url,flush=True)
  links=page.locator('a').evaluate_all('(els)=>els.map(x=>({text:(x.textContent||"").trim(),href:String(x.href||"")})).filter(x=>x.text.toLowerCase().includes("pdf")||x.href.toLowerCase().includes("download"))')
  print(json.dumps(links,ensure_ascii=False)[:5000],flush=True)
 b.close()
