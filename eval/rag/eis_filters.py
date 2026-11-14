from playwright.sync_api import sync_playwright
import json,time,random
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,channel='msedge');page=b.new_page(locale='ru-RU');page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6));page.locator('input[name=searchString]').first.fill('лифты');page.locator('input[name=searchString]').first.press('Enter');page.wait_for_timeout(5000)
 fields=page.locator('input[type=checkbox]').evaluate_all("es=>es.map(e=>({id:e.id,name:e.name,checked:e.checked,label:e.parentElement.innerText.trim().slice(0,120)}))")
 print(json.dumps(fields,ensure_ascii=False),flush=True)
 b.close()
