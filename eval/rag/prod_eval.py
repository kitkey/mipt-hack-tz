"""Как в отчёте проверки: замечание -> evidence_for по правилам enrich (статья из norm_ids, обозначение стандарта,
открытый поиск только с TZCHECK_RAG_OPEN=1). Считает, сколько пунктов приложено верно, неверно и сколько отказов.

Запуск: TZCHECK_REFDATA_OFFLINE=1 python eval/rag/prod_eval.py --modes bm25,hybrid [--open]
Выход: eval/rag/prod_metrics.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(ROOT)

import tzcheck.rag as rag  # noqa: E402
from run_eval import match  # noqa: E402

OPEN_CATEGORIES = ("Стандарты (ГОСТ, поверка)", "Ограничение конкуренции", "Гарантийные условия")


def run(mode, allow_open):
    os.environ["TZCHECK_RAG_MODE"] = mode
    rag._searcher.cache_clear()
    search = rag._searcher(*rag.default_mode())
    qs = json.load(open("eval/rag/questions_v2.json", encoding="utf-8"))
    out = {}
    t = time.time()
    for set_name in ("правила", "правки"):
        c = {"positives": 0, "negatives": 0, "attached_correct": 0, "attached_wrong": 0, "refused_pos": 0, "refused_neg": 0,
             "attached_neg": 0, "skipped_category": 0}
        for q in (x for x in qs if x["set"] == set_name):
            c["negatives" if q["negative"] else "positives"] += 1
            if q["category"] not in rag.ENRICH_CATEGORIES and not q["norm_ids"]:
                ev = None
                c["skipped_category"] += 1
            else:
                open_search = not rag.law_targets(q["norm_ids"]) and not rag.mentioned_documents(q["query"], search.rows)
                if open_search and (not allow_open or q["category"] not in OPEN_CATEGORIES):
                    ev = None
                else:
                    ev = rag.evidence_for(q["query"], q["norm_ids"], search)
            if q["negative"]:
                c["refused_neg" if not ev else "attached_neg"] += 1
            elif not ev:
                c["refused_pos"] += 1
            else:
                c["attached_correct" if any(match(ev, g) for g in q["gold"]) else "attached_wrong"] += 1
        att = c["attached_correct"] + c["attached_wrong"] + c["attached_neg"]
        c["precision_of_attached"] = round(c["attached_correct"] / att, 3) if att else None
        c["coverage_of_positives"] = round(c["attached_correct"] / max(1, c["positives"]), 3)
        out[set_name] = c
    out["seconds_per_query"] = round((time.time() - t) / len(qs), 3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="bm25,hybrid")
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()
    path = Path("eval/rag/prod_metrics.json")
    res = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for mode in a.modes.split(","):
        key = mode + (" + открытый поиск" if a.open else "")
        res[key] = run(mode, a.open)
        print(key, json.dumps(res[key], ensure_ascii=False), flush=True)
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
