from playwright.sync_api import sync_playwright
import time, pathlib
out=pathlib.Path('eval/ocr_compare/browser_probe');out.mkdir(exist_ok=True)
with sync_playwright() as p:
 b=p.chromium.launch(headless=False, executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
 page=b.new_page(locale='ru-RU',accept_downloads=True)
 for name,url in [('search','https://www.google.com/search?q=zakupki.gov.ru+223+%D1%82%D0%B5%D1%85%D0%BD%D0%B8%D1%87%D0%B5%D1%81%D0%BA%D0%BE%D0%B5+%D0%B7%D0%B0%D0%B4%D0%B0%D0%BD%D0%B8%D0%B5'),('eis','https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=%D1%82%D0%B5%D1%85%D0%BD%D0%B8%D1%87%D0%B5%D1%81%D0%BA%D0%BE%D0%B5+%D0%B7%D0%B0%D0%B4%D0%B0%D0%BD%D0%B8%D0%B5&morphology=on&search-filter=Дате+размещения&fz223=on'),('mos','https://zakupki.mos.ru/'),('b2b','https://www.b2b-center.ru/market/'),('rts','https://www.rts-tender.ru/')]:
  try:
   r=page.goto(url,wait_until='domcontentloaded',timeout=35000);page.wait_for_timeout(5000)
   (out/(name+'.txt')).write_text(page.locator('body').inner_text()[:15000],encoding='utf-8');page.screenshot(path=str(out/(name+'.png')))
   print(name, r.status if r else None,page.url,flush=True)
  except Exception as e: print(name,repr(e),flush=True)
 b.close()
