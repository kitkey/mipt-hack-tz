"""Набор вопросов v3: те же 421 вопрос из questions_v2.json, но с раздельными полями замечания
(цитата, объяснение, категория, предмет закупки), чтобы поиск собирал запрос так же, как в отчёте.

Запуск: TZCHECK_REFDATA_OFFLINE=1 python eval/rag/build_questions_v3.py
Выход: eval/rag/questions_v3.json
"""
import csv
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tzcheck.parse import parse  # noqa: E402
from tzcheck.rag.retrieve import subject_of  # noqa: E402

LABEL_DOCS = {"лифт": "samples/lift_src.docx", "метрология": "samples/metro_src.docx"}


def main():
    v2 = json.load(open("eval/rag/questions_v2.json", encoding="utf-8"))
    labels = {x["id"]: x for x in csv.DictReader(open("eval/labels.csv", encoding="utf-8-sig"), delimiter=";")}
    files = {os.path.basename(p): p for p in ["samples/lift_src.docx", "samples/metro_src.docx"] + glob.glob("data_raw/eis_more/*.docx")}
    subjects = {}

    def subject(path):
        if path not in subjects:
            d = parse(path)
            subjects[path] = subject_of(d.fields.get("заголовок", ""), d.text()[:4000])
        return subjects[path]

    # цитата и объяснение находок правил: повторный прогон правил, сопоставление по тексту запроса v2
    split, quotes = {}, {}
    from tzcheck.pipeline import check
    for name, p in files.items():
        r = check(p, use_llm=False)
        for f in r.get("findings", []):
            split[(name, f["quote"].strip() + " " + f["explanation"].strip())] = (f["quote"].strip(), f["explanation"].strip())
            quotes.setdefault(name, set()).add(f["quote"].strip())

    def by_quote(name, query):
        """Правила с тех пор поправили текст объяснения: цитата та же, объяснение берётся из хвоста запроса v2."""
        best = max((x for x in quotes.get(name, ()) if x and query.startswith(x + " ")), key=len, default=None)
        return (best, query[len(best):].strip()) if best else None
    out, unmatched = [], 0
    for q in v2:
        q = dict(q)
        if q["set"] == "правки":
            x = labels[q["id"]]
            q["quote"] = re.sub(r"\s*\|\|\s*", " | ", x["было"]).strip()
            q["explanation"] = x["почему ошибка"].strip()
            q["subject"] = subject(LABEL_DOCS[x["документ"]])
        else:
            name = q["id"].split(":", 1)[0]
            pair = split.get((name, q["query"])) or by_quote(name, q["query"])
            if pair is None:
                unmatched += 1
                pair = (q["query"], "")
            q["quote"], q["explanation"] = pair
            q["subject"] = subject(files[name]) if name in files else ""
        out.append(q)
    json.dump(out, open("eval/rag/questions_v3.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(len(out), "вопросов; без разбиения на цитату и объяснение:", unmatched)
    print(json.dumps(sorted(set(subjects.values())), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
