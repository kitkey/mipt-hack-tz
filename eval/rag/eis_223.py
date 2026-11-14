from playwright.sync_api import sync_playwright
import time,random,json
from pathlib import Path
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,channel='msedge');page=b.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6))
 page.locator('input[name=searchString]').first.fill('лифты');page.locator('input[name=searchString]').first.press('Enter');page.wait_for_timeout(5000)
 page.locator('label[for=fz44]').click();time.sleep(random.uniform(3,6));page.get_by_role('button',name='Применить').first.click();page.wait_for_timeout(6000)
 links=page.locator('a[href]').evaluate_all("es=>es.map(e=>({text:(e.innerText||'').trim(),href:e.href})).filter(x=>/^№\\s*\\d+/.test(x.text)).slice(0,10)")
 print('found',len(links),json.dumps(links,ensure_ascii=False),flush=True)
 Path('eval/rag/eis_223_links.json').write_text(json.dumps(links,ensure_ascii=False,indent=2),encoding='utf8')
 if links:
  page.goto(links[0]['href'],wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6))
  stuff=page.locator('a[href]').evaluate_all("es=>es.map(e=>({text:(e.innerText||'').trim().slice(0,150),href:e.href})).filter(x=>/документ|скачать|docx|\.zip|\.pdf/i.test(x.text+' '+x.href)).slice(0,50)")
  Path('eval/rag/eis_notice_links.json').write_text(json.dumps(stuff,ensure_ascii=False,indent=2),encoding='utf8');print('attachments',json.dumps(stuff,ensure_ascii=False)[:3000],flush=True)
 b.close()
