from playwright.sync_api import sync_playwright
import json
with sync_playwright() as p:
 b=p.chromium.launch(headless=False,executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
 page=b.new_page(locale='ru-RU',accept_downloads=True)
 page.goto('https://zakupki.gov.ru/',wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(4000)
 print('HOME',page.title(),flush=True)
 page.goto('https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=%D1%82%D0%B5%D1%85%D0%BD%D0%B8%D1%87%D0%B5%D1%81%D0%BA%D0%BE%D0%B5+%D0%B7%D0%B0%D0%B4%D0%B0%D0%BD%D0%B8%D0%B5&fz223=on',wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(4000)
 print(json.dumps(page.locator('a').evaluate_all('(a)=>a.map(x=>({text:(x.textContent||"").trim(),href:String(x.href || "")})).filter(x=>x.href.includes("notice")||x.text.includes("326165"))').copy()[:15],ensure_ascii=False),flush=True)
 b.close()


