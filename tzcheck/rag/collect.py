"""Download explicitly catalogued public copies; source versions are never assumed current."""
import json
from datetime import date
from pathlib import Path
import requests
from lxml import html

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'norm_corpus'
SOURCES = [
 ('gost-34441-2018','ГОСТ 34441-2018','2018','https://meganorm.ru/Data2/1/4293733/4293733875.htm'),
 ('gost-r-55964-2014','ГОСТ Р 55964-2014','2014','https://meganorm.ru/Data2/1/4293773/4293773290.htm'),
 ('tr-ts-011-2011','ТР ТС 011/2011','2011','https://meganorm.ru/Data2/1/4293800/4293800086.htm'),
 ('sp-54-13330-2016','СП 54.13330.2016','2016','https://meganorm.ru/Data2/1/4293750/4293750335.htm'),
 ('fz-102','102-ФЗ','ред. 08.08.2024','https://meganorm.ru/mega_doc/dop_fire_update_02082025/postanovlenie_vosemnadtsatogo_arbitrazhnogo_apellyatsionnogo_444/0/federalnyy_zakon_ot_26_06_2008_N_102-fz_red_ot_08_08_2024_ob.html'),
 ('fz-223','223-ФЗ','редакция не установлена','https://meganorm.ru/Data2/1/4293780/4293780931.htm'),
 ('gk2','ГК РФ часть вторая','ред. 24.06.2025, изм. 16.12.2025','https://meganorm.ru/mega_doc/FZ/federalnyj-zakon/0/grazhdanskiy_kodeks_rossiyskoy_federatsii_chast_vtoraya_ot.html'),
 ('gost-34581-2019','ГОСТ 34581-2019','2019','https://meganorm.ru/mega_doc/norm/gost_gosudarstvennyj-standart/1/gost_34581-2019_EN_81-21_2018_mezhgosudarstvennyy_standart.html'),
 ('gost-33984-1-2023','ГОСТ 33984.1-2023','2023','https://meganorm.ru/mega_doc/norm/gost_gosudarstvennyj-standart/0/gost_33984_1-2023_mezhgosudarstvennyy_standart_lifty_lifty.html'),
]
def main():
 ROOT.mkdir(parents=True,exist_ok=True)
 manifest=[]
 for key,title,edition,url in SOURCES:
  try:
   r=requests.get(url,timeout=35,headers={'User-Agent':'Mozilla/5.0'}); r.raise_for_status()
   tree=html.fromstring(r.content)
   for el in tree.xpath('//script|//style|//nav|//footer'): el.drop_tree()
   bodies=tree.xpath('//div[contains(@class,"WordSection")]')
   mega=tree.xpath('//div[@class="contener_doc"]')
   body=bodies[0] if bodies else (mega[0] if mega else tree.xpath('//body')[0])
   blocks=[]
   elements = body.xpath('./div[starts-with(@class,"s")] | ./table') if mega else body.xpath('.//p[not(ancestor::table)] | .//h1[not(ancestor::table)] | .//h2[not(ancestor::table)] | .//h3[not(ancestor::table)] | .//table[not(ancestor::table)]')
   for el in elements:
    if el.tag == 'table':
     lines=[' | '.join(' '.join(c.text_content().split()) for c in row.xpath('./th|./td')) for row in el.xpath('.//tr')]
     value='\n'.join(x for x in lines if x.strip(' |'))
    else: value=' '.join(el.text_content().split())
    if value: blocks.append({'type':'table' if el.tag=='table' else 'text','text':value})
   if sum(len(b['text']) for b in blocks)<1500: raise ValueError('empty or incomplete HTML')
   item={'id':key,'document':title,'edition':edition,'edition_date':None,'status':'не проверен','source':url,'retrieved':date.today().isoformat(),'blocks':blocks}
   (ROOT/(key+'.json')).write_text(json.dumps(item,ensure_ascii=False,indent=2),encoding='utf8')
   manifest.append({'id':key,'ok':True,'blocks':len(blocks),'characters':sum(len(b['text']) for b in blocks)})
  except Exception as e:
   manifest.append({'id':key,'ok':False,'error':str(e)[:200]})
  print(manifest[-1],flush=True)
 (ROOT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
if __name__=='__main__': main()
