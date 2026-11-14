from playwright.sync_api import sync_playwright
import json,time,random
from pathlib import Path
out=Path('eval/rag/eis_browser_result.json')
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=False,channel='msedge')
  page=browser.new_page(locale='ru-RU')
  res=page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=45000)
  time.sleep(random.uniform(3,6))
  inputs=page.locator('input:visible').all()
  fields=[{'placeholder':x.get_attribute('placeholder'),'name':x.get_attribute('name'),'type':x.get_attribute('type')} for x in inputs[:12]]
  print('title:',page.title(),'status:',res.status if res else None,'inputs:',json.dumps(fields,ensure_ascii=False),flush=True)
  output={'status':res.status if res else None,'url':page.url,'title':page.title(),'inputs':fields,'body':page.locator('body').inner_text()[:2000]}
  if inputs:
   field=next((x for x in inputs if x.get_attribute('type') not in ('hidden','checkbox','radio')),None)
   if field:
    field.fill('лифты ГОСТ');time.sleep(random.uniform(3,6));field.press('Enter');page.wait_for_timeout(5000)
    output.update({'search_url':page.url,'search_title':page.title(),'search_body':page.locator('body').inner_text()[:4000]})
  out.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf8')
  browser.close()
except Exception as e:
 out.write_text(json.dumps({'error':type(e).__name__,'message':str(e)[:500]},ensure_ascii=False,indent=2),encoding='utf8');print(type(e).__name__,str(e)[:300],flush=True)
