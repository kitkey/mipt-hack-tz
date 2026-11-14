from playwright.sync_api import sync_playwright
import time,random,json
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,channel='msedge');page=b.new_page(locale='ru-RU');page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=45000);time.sleep(random.uniform(3,6));page.locator('input[name=searchString]').first.fill('лифты');page.locator('input[name=searchString]').first.press('Enter');page.wait_for_timeout(5000)
 print('filter_form',page.locator('#fz44').evaluate("e=>e.closest('form')?.outerHTML.slice(-7000)"),flush=True)
 b.close()
