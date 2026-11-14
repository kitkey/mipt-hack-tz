from playwright.sync_api import sync_playwright
import time,random,json
from pathlib import Path
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,channel='msedge');page=b.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6))
 page.locator('input[name=searchString]').first.fill('лифты ГОСТ');page.locator('input[name=searchString]').first.press('Enter');page.wait_for_timeout(6000)
 links=page.locator('a[href]').evaluate_all("els => els.map(e => ({text:(e.innerText || '').trim().slice(0,160),href:e.href})).filter(x => x.text && /\u2116|223-ФЗ/.test(x.text)).slice(0,40)")
 print(json.dumps(links,ensure_ascii=False),flush=True)
 Path('eval/rag/eis_links.json').write_text(json.dumps(links,ensure_ascii=False,indent=2),encoding='utf8');b.close()
