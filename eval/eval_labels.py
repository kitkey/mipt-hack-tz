import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZCHECK_REFDATA_OFFLINE", "1")
from tzcheck.pipeline import check

DOCS = {"лифт": "samples/lift_src.docx", "метрология": "samples/metro_src.docx"}
FINAL = {"лифт": "samples/lift_fin.docx", "метрология": "samples/metro_fin.docx"}


def norm(s):
    return re.sub(r"\s+", " ", s.replace("||", "|")).strip().lower()


def find_blocks(doc, was):
    w = norm(was)
    keys = [w[:60], w[:35]] + [x.strip()[:40] for x in w.split("|") if len(x.strip()) > 12]
    hits = []
    for b in doc.blocks:
        t = norm(b.text)
        if any(k and k in t for k in keys):
            hits.append(b.id)
    return hits


def quote_hit(f, was):
    w = set(re.findall(r"\w{3,}", norm(was)))
    q = set(re.findall(r"\w{3,}", f["quote"].lower()))
    if not w or not q:
        return False
    return len(w & q) >= min(3, len(w)) or norm(f["quote"])[:40] in norm(was) or norm(was)[:40] in norm(f["quote"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="eval/labels.csv")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--out", default="eval/result.json")
    args = ap.parse_args()
    labels = list(csv.DictReader(open(args.labels, encoding="utf-8-sig"), delimiter=";"))
    total, hit, hit_cat, strict = Counter(), Counter(), Counter(), Counter()
    unlocated = 0
    report = {}
    misses = []
    label_results = []
    for name, path in DOCS.items():
        res = check(path, use_llm=args.llm)
        doc = res["_doc"]
        by_block = defaultdict(list)
        for f in res["findings"]:
            for b in [f["block_id"]] + f.get("also_in", []):
                by_block[b].append(f)
        matched_findings = set()
        for lab in (x for x in labels if x["документ"] == name):
            cls = lab["класс"]
            total[cls] += 1
            blocks = find_blocks(doc, lab["было"])
            if not blocks:
                unlocated += 1
                label_results.append({"doc": name, "class": cls, "located": False, "block": False, "category": False, "strict": False})
                continue
            fs = [f for b in blocks for f in by_block.get(b, [])]
            label_results.append({"doc": name, "class": cls, "located": True, "block": bool(fs),
                                  "category": any(f["category"] == cls for f in fs),
                                  "strict": any(f["category"] == cls and quote_hit(f, lab["было"]) for f in fs)})
            if fs:
                hit[cls] += 1
                matched_findings.update(f["id"] for f in fs)
                if any(f["category"] == cls for f in fs):
                    hit_cat[cls] += 1
                if any(f["category"] == cls and quote_hit(f, lab["было"]) for f in fs):
                    strict[cls] += 1
                    continue
            misses.append({"doc": name, "class": cls, "was": lab["было"][:200],
                           "found": [(f["category"], f["source"], f["quote"][:80]) for f in fs]})
        fin = check(FINAL[name], use_llm=args.llm)
        report[name] = {"findings_src": len(res["findings"]), "matched_findings": len(matched_findings),
                        "findings_final": len(fin["findings"]),
                        "high_final": sum(1 for f in fin["findings"] if f["severity"] == "high")}
    allt, allh, allc, alls = sum(total.values()), sum(hit.values()), sum(hit_cat.values()), sum(strict.values())
    print(f"Метки: {allt}, не найдено место в документе: {unlocated}")
    print(f"Полнота по блокам: {allh}/{allt} = {allh / allt:.0%}; с верной категорией: {allc}/{allt} = {allc / allt:.0%}; "
          f"строго (категория + цитата про это место): {alls}/{allt} = {alls / allt:.0%}")
    for cls in sorted(total, key=lambda c: -total[c]):
        print(f"  {cls}: блок {hit[cls]}/{total[cls]}, категория {hit_cat[cls]}, строго {strict[cls]}")
    for name, r in report.items():
        print(f"{name}: замечаний на исходном {r['findings_src']}, из них у размеченных мест {r['matched_findings']}; на итоговом {r['findings_final']} (высокий {r['high_final']})")
    json.dump({"total": dict(total), "hit": dict(hit), "hit_cat": dict(hit_cat), "strict": dict(strict), "docs": report, "misses": misses, "label_results": label_results}, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
