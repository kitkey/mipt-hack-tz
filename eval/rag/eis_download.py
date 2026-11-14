from playwright.sync_api import sync_playwright
import json,time,random,re
from pathlib import Path
DATA=Path('eval/rag/eis_attachments.json');DEST=Path('eval/rag/eis_documents');DEST.mkdir(exist_ok=True)
choices=[]
for row in json.loads(DATA.read_text(encoding='utf8')):
 for item in row.get('items',[]):
  if '/filestore/' in item['href'] and re.search('описание предмета|техническое задание|Документация №',item['text'],re.I): choices.append((row['notice'],row['url'],item))
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,channel='msedge');page=b.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6));page.locator('input[name=searchString]').first.fill('лифты');page.locator('input[name=searchString]').first.press('Enter');page.wait_for_timeout(5000)
 page.locator('label[for=fz44]').click();time.sleep(random.uniform(3,6));page.get_by_role('button',name='Применить').first.click();page.wait_for_timeout(5000)
 results=[]
 for i,(notice,docs,item) in enumerate(choices):
  try:
   page.goto(notice,wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6));page.goto(docs,wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6))
   with page.expect_download(timeout=40000) as download_info: page.locator('a[href="'+item['href']+'"]').first.click()
   dl=download_info.value;ext=Path(dl.suggested_filename).suffix.lower();ext=ext if ext in ('.docx','.doc','.pdf','.txt','.xlsx','.zip') else '.bin'
   dst=DEST/('eis_'+str(i)+ext);dl.save_as(dst)
   results.append({'notice':notice,'attachment':item['text'],'source':item['href'],'file':str(dst),'bytes':dst.stat().st_size});print('downloaded',dst,dst.stat().st_size,flush=True)
  except Exception as e: results.append({'notice':notice,'error':type(e).__name__});print('download error',type(e).__name__,flush=True)
 Path('eval/rag/eis_downloads.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8');b.close()
