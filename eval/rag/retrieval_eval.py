"""Свободный поиск пунктов норм (без статьи из norm_ids): hit@1 и hit@5 по вариантам сборки запроса и поиска.

Варианты: прежний запрос (цитата + объяснение) по всему корпусу; запрос с предметом закупки; две дорожки
(законы и стандарты); дорожки с правовой темой категории; всё это для BM25, гибрида и гибрида с реранкером.

Запуск: TZCHECK_REFDATA_OFFLINE=1 python eval/rag/retrieval_eval.py [--device cuda] [--rerank]
Вход: eval/rag/questions_v3.json. Выход: eval/rag/retrieval_metrics.json
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

from tzcheck.rag import Search, INDEX  # noqa: E402
from tzcheck.rag import models as M  # noqa: E402
from tzcheck.rag.retrieve import candidates  # noqa: E402
from run_eval import match  # noqa: E402

CHAR = [["223-ФЗ", "Статья 3, ч. 6.1"], ["223-ФЗ", "Статья 4, ч. 10"]]
VARIANTS = {
    "прежний запрос, весь корпус": dict(use_lanes=False, use_topic=False, subject=False),
    "+ предмет закупки, весь корпус": dict(use_lanes=False, use_topic=False, subject=True),
    "+ предмет, две дорожки": dict(use_lanes=True, use_topic=False, subject=True),
    "+ предмет, две дорожки, тема категории": dict(use_lanes=True, use_topic=True, subject=True),
    "+ предмет, две дорожки, тема категории, порядок по оценке": dict(use_lanes=True, use_topic=True, subject=True, order="score"),
    "тема категории, весь корпус": dict(use_lanes=False, use_topic=True, subject=True),
}


def first_hit(cands, gold):
    for r, h in enumerate(cands, 1):
        if any(match(h, g) for g in gold):
            return r
    return None


def subsets(qs):
    pos = [q for q in qs if not q["negative"]]
    return {
        "правки": [q for q in pos if q["set"] == "правки"],
        "правки, эталон по правке": [q for q in pos if q["set"] == "правки" and q["gold"] != CHAR],
        "правки, эталон по категории": [q for q in pos if q["set"] == "правки" and q["gold"] == CHAR],
        "правила без norm_ids": [q for q in pos if q["set"] == "правила"],
    }


def run(search, qs, v):
    ranks, t = {}, time.time()
    for q in qs:
        c = candidates(search, q["quote"], q["explanation"], q["category"], q["subject"] if v["subject"] else "",
                       k=10, use_lanes=v["use_lanes"], use_topic=v["use_topic"], order=v.get("order", "interleave"))
        ranks[q["id"]] = (first_hit(c, q["gold"]), [[h["document"], h["number"]] for h in c[:5]])
    return ranks, (time.time() - t) / max(1, len(qs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--modes", default="bm25,hybrid")
    a = ap.parse_args()
    os.environ["TZCHECK_RAG_DEVICE"] = a.device
    qs = json.load(open("eval/rag/questions_v3.json", encoding="utf-8"))
    pos = [q for q in qs if not q["negative"]]
    modes = a.modes.split(",") + (["rerank"] if a.rerank else [])
    res, detail = {}, {}
    for mode in modes:
        search = Search(INDEX) if mode == "bm25" else M.HybridSearch(M.DEFAULT_MODEL, mode)
        if getattr(search, "cross", None) is not None and a.device != "cpu":
            # пауза после каждой пачки реранкера: средняя загрузка GPU не выше ~50 %
            predict = search.cross.predict

            def slow(*args, _p=predict, **kw):
                t = time.time()
                r = _p(*args, **kw)
                time.sleep(time.time() - t)
                return r
            search.cross.predict = slow
        for name, v in VARIANTS.items():
            if mode == "bm25" and v.get("order") == "score":
                continue
            ranks, sec = run(search, pos, v)
            key = f"{mode}: {name}"
            res[key] = {"с/вопрос": round(sec, 3)}
            for sname, sub in subsets(qs).items():
                rr = [ranks[q["id"]][0] for q in sub]
                res[key][sname] = {"n": len(rr), "hit@1": sum(1 for r in rr if r == 1), "hit@5": sum(1 for r in rr if r and r <= 5)}
            detail[key] = {qid: r for qid, r in ranks.items()}
            print(key, json.dumps(res[key], ensure_ascii=False), flush=True)
        del search
    json.dump({"metrics": res, "ranks": detail}, open("eval/rag/retrieval_metrics.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
