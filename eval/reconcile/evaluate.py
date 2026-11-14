"""Измерение только новых срабатываний reconcile на доступных исходниках."""
import json
import os
from pathlib import Path
import sys
import tempfile
import docx
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tzcheck.parse import parse_many
from tzcheck.reconcile import reconcile

ROOT = Path(__file__).resolve().parents[2]
os.environ['TZCHECK_REFDATA_OFFLINE'] = '1'
inputs = [ROOT / 'samples/lift_fin.docx', ROOT / 'samples/metro_fin.docx']
eis = Path(os.environ.get("TZCHECK_EIS_DIR", Path(__file__).resolve().parents[2] / "data_raw" / "eis_more"))
inputs += sorted(eis.glob('*.docx'))[:3]
cases = {'значение': ('Контрольная масса: 10 кг', 'Контрольная масса: 20 кг'),
         'диапазон': ('Контрольная длина: не менее 10 м', 'Контрольная длина: не более 5 м'),
         'срок': ('Срок гарантии: 12 месяцев', 'Срок гарантии: 24 месяца'),
         'количество': ('Контрольное количество: 2 шт', 'Контрольное количество: 4 шт')}
rows=[]
baselines={}
with tempfile.TemporaryDirectory() as tmp:
    for src in inputs:
        baseline = reconcile(parse_many([src]))
        baselines[src.name] = [{"категория": f.category, "цитата": f.quote, "довод": f.explanation} for f in baseline]
        for typ, (a,b) in cases.items():
            dst = Path(tmp)/src.name
            d=docx.Document(src); d.add_paragraph(a); d.save(dst)
            attachment = Path(tmp) / "Приложение 99.docx"
            d2 = docx.Document(); d2.add_paragraph(b); d2.save(attachment)
            found = reconcile(parse_many([dst, attachment]))
            hit = any(f.source == 'reconcile' and a in f.quote and b in f.evidence.get('вторая цитата','') for f in found)
            rows.append({'файл':src.name,'вид':typ,'найдено':hit})
        print(src.name, 'исходные замечания сверки:', len(baseline), 'найдено:', sum(r['найдено'] for r in rows[-len(cases):]), '/',len(cases))
        for f in baseline[:4]: print(' ', f.category, f.quote[:100])
    print('ИТОГО',sum(r['найдено'] for r in rows),'/',len(rows))
    Path(__file__).with_name('result.json').write_text(json.dumps({'cases': rows, 'baseline': baselines}, ensure_ascii=False, indent=2),encoding='utf-8')
