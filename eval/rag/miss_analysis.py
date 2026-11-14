"""Почему свободный поиск промахивается на 77 правках с ответом: ранги эталона у BM25, эмбеддингов и гибрида,
путь замечания в отчёте (категория, порог отказа), есть ли документ эталона в выдаче вообще.

Запуск: TZCHECK_REFDATA_OFFLINE=1 python eval/rag/miss_analysis.py [--device cuda]
Выход: eval/rag/miss_analysis.json
"""
import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

import tzcheck.rag as rag  # noqa: E402
from tzcheck.rag import models as M  # noqa: E402
from run_eval import match  # noqa: E402

LAWS = {"223-ФЗ", "102-ФЗ", "ГК РФ часть вторая"}


def rank(order, rows, gold, doc_level=False, depth=100):
    for r, i in enumerate(order[:depth], 1):
        if any(match(rows[i], (g[0], None) if doc_level else g) for g in gold):
            return r
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    a = ap.parse_args()
    os.environ["TZCHECK_RAG_DEVICE"] = a.device
    qs = [q for q in json.load(open("eval/rag/questions_v2.json", encoding="utf-8")) if q["set"] == "правки" and not q["negative"]]
    hy = M.HybridSearch(M.DEFAULT_MODEL, "hybrid")
    rows = hy.rows
    lex = hy.lex
    out, reasons = [], Counter()
    for q in qs:
        docs = rag.mentioned_documents(q["query"], rows)
        allowed = lex.allowed(docs) if docs else None
        bm = lex.scores(q["query"], allowed)
        bm_order = sorted(bm, key=lambda i: -bm[i])
        dense = hy.dense_scores(q["query"])
        import numpy as np
        if allowed is not None:
            mask = np.zeros(len(rows), dtype=bool)
            mask[list(allowed)] = True
            dense = np.where(mask, dense, -1.0)
        d_order = [int(i) for i in np.argsort(-dense)[:100]]
        h_order, gate = hy.ranked(q["query"], allowed)
        gold_docs = {g[0] for g in q["gold"]}
        rec = {"id": q["id"], "category": q["category"], "gold": q["gold"], "mentioned": sorted(docs),
               "open_category": q["category"] in rag.OPEN_CATEGORIES,
               "bm25": rank(bm_order, rows, q["gold"]), "dense": rank(d_order, rows, q["gold"]), "hybrid": rank(h_order, rows, q["gold"]),
               "bm25_doc": rank(bm_order, rows, q["gold"], True), "hybrid_doc": rank(h_order, rows, q["gold"], True),
               "hybrid_top1": [rows[h_order[0]]["document"], rows[h_order[0]]["number"]] if h_order else None,
               "gate": round(gate.get(h_order[0], -1), 3) if h_order else None,
               "law_rows_in_top10": sum(rows[i]["document"] in LAWS for i in h_order[:10]),
               "gold_is_law": gold_docs <= LAWS}
        # Причина промаха в отчёте (как в enrich: открытый поиск только для трёх категорий и выше порога)
        if docs:
            why = "стандарт назван, поиск внутри документа" + (" нашёл" if rec["hybrid"] == 1 else " промахнулся по пункту")
        elif not rec["open_category"]:
            why = "категория вне открытого поиска"
        elif rec["gate"] is None or rec["gate"] < hy.refuse_threshold:
            why = "ниже порога отказа"
        elif rec["hybrid"] == 1:
            why = "нашёл"
        else:
            why = "выдача: первый не эталон"
        rec["prod_reason"] = why
        # Причина промаха поиска как такового (ранг гибрида по всему корпусу)
        if rec["hybrid"] == 1:
            rr = "top1"
        elif rec["hybrid"] and rec["hybrid"] <= 5:
            rr = "top2-5"
        elif rec["hybrid_doc"] and rec["hybrid_doc"] <= 10:
            rr = "документ в топ-10, пункт ниже"
        elif rec["gold_is_law"] and rec["law_rows_in_top10"] == 0:
            rr = "эталон закон, в топ-10 только ГОСТ/СП"
        else:
            rr = "эталон вне топ-10"
        rec["search_reason"] = rr
        reasons[(why, rr)] += 1
        out.append(rec)
    distinct = len({q["query"] for q in qs})
    summary = {"questions": len(qs), "distinct_queries": distinct,
               "prod_reason": Counter(r["prod_reason"] for r in out), "search_reason": Counter(r["search_reason"] for r in out),
               "by_gold": Counter(json.dumps(r["gold"], ensure_ascii=False) for r in out),
               "hit@1": {m: sum(1 for r in out if r[m] == 1) for m in ("bm25", "dense", "hybrid")},
               "hit@5": {m: sum(1 for r in out if r[m] and r[m] <= 5) for m in ("bm25", "dense", "hybrid")},
               "hit@100": {m: sum(1 for r in out if r[m]) for m in ("bm25", "dense", "hybrid")},
               "top1_documents": Counter(r["hybrid_top1"][0] for r in out if r["hybrid_top1"]).most_common(15)}
    json.dump({"summary": summary, "rows": out}, open("eval/rag/miss_analysis.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
