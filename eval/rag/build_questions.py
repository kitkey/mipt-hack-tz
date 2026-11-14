"""Набор вопросов к поиску норм: 105 правок закупщиков (ручная разметка нужного пункта) и находки правил.

Запуск: TZCHECK_REFDATA_OFFLINE=1 .venv/Scripts/python eval/rag/build_questions.py
Пишет eval/rag/questions_v2.json: [{id, set, query, norm_ids, gold: [[документ, пункт|null], ...], negative}]
gold с пунктом null = достаточно верного документа; negative = в корпусе нет ответа, правильно отказать.
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

COMP = [["223-ФЗ", "Статья 3, ч. 6.1"]]
SI = [["102-ФЗ", "Статья 13, ч. 1"], ["102-ФЗ", "Статья 9, ч. 1"]]
CHAR = [["223-ФЗ", "Статья 3, ч. 6.1"], ["223-ФЗ", "Статья 4, ч. 10"]]
# Ручная разметка: какой пункт нормы из корпуса должен прийти на правку закупщика.
LABEL_GOLD = {
    **{k: COMP for k in ["M-013", "M-014", "M-015", "M-016", "M-024", "M-025", "M-026", "M-027", "M-038", "M-039",
                         "M-040", "M-041", "M-042", "M-043", "M-044", "M-045", "M-046", "M-047", "M-048", "M-049", "M-069"]},
    **{k: SI for k in ["M-034", "M-035", "M-052", "M-053", "M-054", "M-055", "M-056", "M-057", "M-058", "M-059",
                       "M-060", "M-061", "M-062", "M-063"]},
    "M-051": [["223-ФЗ", "Статья 3, ч. 8"]],
    "M-088": [["223-ФЗ", "Статья 3, ч. 8"]],
    "L-008": [["ГК РФ часть вторая", "Статья 722"], ["ГК РФ часть вторая", "Статья 724"], ["ГК РФ часть вторая", "Статья 755"],
              ["ГК РФ часть вторая", "Статья 756"]],
    "M-089": [["ГК РФ часть вторая", "Статья 471"], ["ГК РФ часть вторая", "Статья 470"]],
    "M-090": [["ГК РФ часть вторая", "Статья 475"], ["ГК РФ часть вторая", "Статья 470"]],
    "L-009": [["ГОСТ Р 53782-2010", None]],
    "L-010": [["ГОСТ Р 53783-2010", None]],
    "L-004": [["ГОСТ Р 53782-2010", None], ["ТР ТС 011/2011", None]],
    "M-073": [["ГОСТ 6507-90", None]],
}
# Правки, ответа на которые в корпусе нет: ОКПД 2, внутренний стандарт, внутренние противоречия,
# стандарты, которых нет в корпусе (весы OIML R 76, метод Роквелла, штангенрейсмас, язык документации).
LABEL_NEGATIVE = {"L-001", "L-002", "L-003", "L-005", "L-006", "L-007", "L-012", "L-013", "M-002", "M-003", "M-004",
                  "M-005", "M-006", "M-007", "M-008", "M-009", "M-010", "M-011", "M-064", "M-072", "M-083", "M-087"}
CHAR_CLASSES = {"Неполнота или комплектность", "Некорректная характеристика"}

NORM_GOLD = {
    "223-FZ-3-6.1": ["223-ФЗ", "Статья 3, ч. 6.1"], "223-FZ-3-1": ["223-ФЗ", "Статья 3, ч. 1"],
    "223-FZ-4-10": ["223-ФЗ", "Статья 4, ч. 10"], "102-FZ-9-1": ["102-ФЗ", "Статья 9, ч. 1"],
    "102-FZ-13-1": ["102-ФЗ", "Статья 13, ч. 1"], "GK-470": ["ГК РФ часть вторая", "Статья 470"],
    "GK-471": ["ГК РФ часть вторая", "Статья 471"], "GK-478": ["ГК РФ часть вторая", "Статья 478"],
    "GK-722": ["ГК РФ часть вторая", "Статья 722"],
}
NOT_IN_CORPUS = {"INTERNAL", "OK-034-2014", "ARSHIN", "ROSSTANDART", "TR-TS-004-5", "GOST-OIML-R76-1"}


def label_questions():
    rows = list(csv.DictReader(open("eval/labels.csv", encoding="utf-8-sig"), delimiter=";"))
    out = []
    for x in rows:
        query = re.sub(r"\s*\|\|\s*", " | ", x["было"]).strip() + " " + x["почему ошибка"].strip()
        gold = LABEL_GOLD.get(x["id"])
        if gold is None and x["класс"] in CHAR_CLASSES:
            gold = CHAR
        negative = x["id"] in LABEL_NEGATIVE
        if gold is None and not negative:
            continue
        out.append({"id": x["id"], "set": "правки", "category": x["класс"], "query": query, "norm_ids": [],
                    "gold": gold or [], "negative": negative})
    return out


def rule_questions():
    from tzcheck.pipeline import check
    from tzcheck.rag import Search, mentioned_documents
    rows = Search().rows
    files = ["samples/lift_src.docx", "samples/metro_src.docx"] + sorted(glob.glob("data_raw/eis_more/*.docx"))
    out, seen = [], set()
    for p in files:
        r = check(p, use_llm=False)
        if r["status"] != "ok":
            continue
        for f in r["findings"]:
            query = f["quote"].strip() + " " + f["explanation"].strip()
            key = re.sub(r"\W+", " ", query.lower())[:300]
            if key in seen:
                continue
            seen.add(key)
            gold = []
            for n in f["norm_ids"]:
                for prefix, target in NORM_GOLD.items():
                    if n.startswith(prefix) and target not in gold:
                        gold.append(target)
            for d in sorted(mentioned_documents(query, rows)):
                gold.append([d, None])
            negative = not gold and (not f["norm_ids"] or set(f["norm_ids"]) <= NOT_IN_CORPUS)
            if not gold and not negative:
                continue
            out.append({"id": f"{os.path.basename(p)}:{f['id']}", "set": "правила", "category": f["category"], "query": query,
                        "norm_ids": f["norm_ids"], "gold": gold, "negative": negative})
    return out


def main():
    qs = label_questions() + rule_questions()
    path = os.path.join(ROOT, "eval", "rag", "questions_v2.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(qs, fh, ensure_ascii=False, indent=1)
    from collections import Counter
    print(len(qs), Counter((q["set"], q["negative"]) for q in qs))


if __name__ == "__main__":
    main()
