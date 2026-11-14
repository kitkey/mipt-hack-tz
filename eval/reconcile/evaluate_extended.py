"""Reproducible mutations on public documents; no API calls."""
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
import docx
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["TZCHECK_REFDATA_OFFLINE"] = "1"
from tzcheck.parse import parse_many
from tzcheck.reconcile import reconcile

EIS = Path(os.environ.get("TZCHECK_EIS_DIR", Path(__file__).resolve().parents[2] / "data_raw" / "eis_more"))
DOCS = [ROOT / "samples/lift_fin.docx", ROOT / "samples/metro_fin.docx", *sorted(EIS.glob("*.docx"))[:3]]
CASES = {
    "значение": ("Контрольная масса: 10 кг", "Контрольная масса: 20 кг"),
    "диапазон": ("Контрольная длина: не менее 10 м", "Контрольная длина: не более 5 м"),
    "количество": ("Контрольное количество: 2 шт", "Контрольное количество: 4 шт"),
    "срок поставки": ("Срок поставки: 01.05.2027", "Срок поставки: 02.05.2027"),
    "гарантия": ("Срок гарантии: 12 месяцев", "Срок гарантии: 24 месяца"),
    "срок приёмки": ("Срок приемки: 3 рабочих дня", "Срок приемки: 8 рабочих дней"),
    "порядок приёмки": ("Форма приемки: только в электронном виде", "Форма приемки: только в бумажном виде"),
    "комплектность": ("В комплект входит: кабель питания.", "Не входит в комплект: кабель питания."),
    "единицы": ("Контрольная масса: 10 кг", "Контрольная масса: 20 мм"),
    "пропавшее приложение": ("См. приложение 999", ""),
    "содержание": ("", ""),
    "дублирование раздела": ("", ""),
}

def mutate(d, attachment, kind, a, b):
    if kind == "содержание":
        attachment.add_heading("СОДЕРЖАНИЕ", level=1)
        attachment.add_paragraph("77 Отсутствующий контрольный раздел .... 77")
        attachment.add_heading("1 Другая глава", level=1)
        attachment.add_paragraph("Содержимое другой главы.")
    elif kind == "дублирование раздела":
        attachment.add_heading("80 Контрольный раздел качества", level=1)
        attachment.add_paragraph("Первый вариант требований к качеству содержит контрольное значение.")
        attachment.add_heading("81 Контрольный раздел качества", level=1)
        attachment.add_paragraph("Второй вариант требований к качеству содержит другое содержание.")
    else:
        d.add_paragraph(a)
        if b:
            attachment.add_paragraph(b)
        else:
            attachment.add_paragraph("Сведения приложения.")

def hit(kind, a, b, findings):
    if kind == "содержание":
        return any("содержания" in f.explanation and "77" in f.quote for f in findings)
    if kind == "дублирование раздела":
        return any("одинаковым названием" in f.explanation and "Контрольный раздел качества" in f.explanation for f in findings)
    if kind == "пропавшее приложение":
        # ссылки одного документа сводятся в одно замечание: номер может быть в перечне объяснения
        return any("999" in f.quote or "приложение 999" in f.explanation for f in findings)
    return any(a in f.quote and b in f.evidence.get("вторая цитата", "") for f in findings)

def main():
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for source in DOCS:
            for kind, (a, b) in CASES.items():
                d = docx.Document(source); attachment = docx.Document()
                mutate(d, attachment, kind, a, b)
                main_path = Path(tmp)/"main.docx"; app_path = Path(tmp)/"Приложение 998.docx"
                d.save(main_path); attachment.save(app_path)
                found = reconcile(parse_many([main_path, app_path]))
                rows.append({"doc": source.name, "type": kind, "found": hit(kind,a,b,found)})
        for kind in CASES:
            subset = [r for r in rows if r["type"] == kind]
            print(kind, sum(r["found"] for r in subset), "/", len(subset))
        print("ИТОГО",sum(r["found"] for r in rows),"/",len(rows))
        Path(__file__).with_name("result_extended.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
