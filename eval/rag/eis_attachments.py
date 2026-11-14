from playwright.sync_api import sync_playwright
import time,random,json
from pathlib import Path
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,channel='msedge'); page=b.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6));page.locator('input[name=searchString]').first.fill('лифты');page.locator('input[name=searchString]').first.press('Enter');page.wait_for_timeout(5000)
 page.locator('label[for=fz44]').click();time.sleep(random.uniform(3,6));page.get_by_role('button',name='Применить').first.click();page.wait_for_timeout(6000)
 links=page.locator('a[href*="/223/purchase/public/purchase/info/common-info.html"]').evaluate_all("es=>es.map(e=>e.href).filter((x,i,a)=>a.indexOf(x)===i).slice(0,3)")
 outputs=[]
 for link in links:
  try:
   page.goto(link,wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6));doc=page.locator('a[href*="documents.html"]').first.get_attribute('href',timeout=10000);page.goto('https://zakupki.gov.ru'+doc if doc.startswith('/') else doc,wait_until='domcontentloaded',timeout=45000);page.wait_for_timeout(5000)
   items=page.locator('a[href]').evaluate_all("es=>es.map(e=>({text:(e.innerText||'').trim().slice(0,180),href:e.href})).filter(x=>/docx|xlsx|pdf|скачать|техническ|тз|\.doc|download/i.test(x.text+' '+x.href)).slice(0,60)")
   outputs.append({'notice':link,'url':page.url,'items':items});print('notice',link,'items',len(items),flush=True)
  except Exception as e: outputs.append({'notice':link,'error':type(e).__name__});print(type(e).__name__,flush=True)
 Path('eval/rag/eis_attachments.json').write_text(json.dumps(outputs,ensure_ascii=False,indent=2),encoding='utf8');b.close()
