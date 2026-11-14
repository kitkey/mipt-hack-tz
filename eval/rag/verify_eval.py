"""Точность приложенных пунктов норм с проверкой моделью и без неё, на 421 вопросе (questions_v3.json).

Шаг 1, кандидаты (без модели, можно на GPU):
  TZCHECK_REFDATA_OFFLINE=1 python eval/rag/verify_eval.py candidates --mode hybrid --device cuda
Шаг 2, решения модели (в Docker с ключом из .env, см. eval/rag/REPORT.md):
  python eval/rag/verify_eval.py judge --mode hybrid
Шаг 3, сводка: python eval/rag/verify_eval.py score --mode hybrid

Отбор замечаний как в enrich: категории ENRICH_CATEGORIES или есть norm_ids. Варианты:
- «прежний путь»: evidence_for (статья, обозначение, открытый поиск выше порога в трёх категориях);
- «первый кандидат без модели»: первый из того же списка кандидатов, что идёт модели;
- «проверка моделью»: первый применимый со сверенной цитатой (verify.pick);
- «по умолчанию»: как в конвейере (verify.enrich_verified), у замечаний правил с известным документом пункт без модели,
  остальное проверка моделью. Набор «правки» считается замечаниями модели: они проверяются всегда.
Шаг judge с --missing спрашивает только вопросы без решений (после сбоя вызовов) и дописывает их к прежним.
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

import tzcheck.rag as rag  # noqa: E402
from tzcheck.rag import verify  # noqa: E402
from run_eval import match  # noqa: E402



def paths(mode):
    return (f"eval/rag/verify_candidates_{mode}.json", f"eval/rag/verify_decisions_{mode}.json", f"eval/rag/verify_metrics_{mode}.json")


CHAR = [["223-ФЗ", "Статья 3, ч. 6.1"], ["223-ФЗ", "Статья 4, ч. 10"]]
CHAR_CLASSES = {"Неполнота или комплектность", "Некорректная характеристика"}


def gold_wide(q):
    """Эталон с поправкой: для замечаний о характеристиках и неполноте обе статьи 223-ФЗ о описании предмета закупки,
    как в разметке правок (build_questions.py, CHAR); в наборе правил эталон иначе только статья из norm_ids правила."""
    return q["gold"] + ([g for g in CHAR if g not in q["gold"]] if q["category"] in CHAR_CLASSES and q["gold"] else [])


def eligible(q):
    return q["category"] in rag.ENRICH_CATEGORIES or bool(q["norm_ids"])


def cmd_candidates(a):
    CAND, _, _ = paths(a.mode)
    from tzcheck.rag import models as M
    from tzcheck.rag.retrieve import candidates
    os.environ["TZCHECK_RAG_DEVICE"] = a.device
    qs = json.load(open("eval/rag/questions_v3.json", encoding="utf-8"))
    search = rag.Search(rag.INDEX) if a.mode == "bm25" else M.HybridSearch(M.DEFAULT_MODEL, a.mode)
    out, t = [], time.time()
    for q in qs:
        rec = {"id": q["id"], "eligible": eligible(q)}
        if rec["eligible"]:
            c = candidates(search, q["quote"], q["explanation"], q["category"], q["subject"], q["norm_ids"], k=a.k)
            rec["candidates"] = [{k: h.get(k) for k in ("id", "document", "number", "title", "text", "status", "source", "edition",
                                                        "replaced_by", "kind", "via", "score", "window_ids")} for h in c]
            text = q["quote"] + " " + q["explanation"]
            open_search = not rag.law_targets(q["norm_ids"]) and not rag.mentioned_documents(text, search.rows)
            for name, allow in (("old", False), ("old_open", True)):
                if open_search and (not allow or q["category"] not in rag.OPEN_CATEGORIES):
                    ev = None
                else:
                    ev = rag.evidence_for(text, q["norm_ids"], search)
                rec[name] = {k: ev[k] for k in ("document", "number", "quote", "how")} if ev else None
        out.append(rec)
    meta = {"mode": a.mode, "k": a.k, "seconds_per_question": round((time.time() - t) / len(qs), 3)}
    json.dump({"meta": meta, "rows": out}, open(CAND, "w", encoding="utf-8"), ensure_ascii=False)
    print(meta)


def cmd_judge(a):
    CAND, JUDGED, _ = paths(a.mode)
    from tzcheck.pii import Scrubber
    qs = {q["id"]: q for q in json.load(open("eval/rag/questions_v3.json", encoding="utf-8"))}
    data = json.load(open(CAND, encoding="utf-8"))
    rows = [r for r in data["rows"] if r["eligible"] and r["candidates"]]
    prev = None
    if a.missing and os.path.exists(JUDGED):
        # дозапрос только вопросов без единого решения (сбой вызова в прошлом прогоне), остальные решения сохраняются
        prev = json.load(open(JUDGED, encoding="utf-8"))
        rows = [r for r in rows if not any(d.get("decision") for d in prev["decisions"].get(r["id"], []))]
    tasks = [{"quote": qs[r["id"]]["quote"], "explanation": qs[r["id"]]["explanation"], "category": qs[r["id"]]["category"],
              "subject": qs[r["id"]]["subject"], "candidates": r["candidates"]} for r in rows]
    t = time.time()
    errors = []

    def safe(fn, arg):
        try:
            return fn(arg)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {str(e)[:150]}")
            return None
    dec = verify.judge(tasks, Scrubber(), safe=safe)
    meta = {"model": os.environ.get("TZCHECK_LLM_MODEL", ""), "seconds": round(time.time() - t, 1), "tasks": len(tasks),
            "errors": errors[:10], "error_count": len(errors), "batch": int(os.environ.get("TZCHECK_RAG_VERIFY_BATCH", "4"))}
    decisions = {r["id"]: d for r, d in zip(rows, dec)}
    if prev is not None:
        pm = prev["meta"]
        meta = dict(pm, missing_rerun={k: meta[k] for k in ("seconds", "tasks", "error_count", "errors")},
                    seconds=round(pm.get("seconds", 0) + meta["seconds"], 1), error_count=len(errors), errors=errors[:10])
        decisions = dict(prev["decisions"], **decisions)
    json.dump({"meta": meta, "decisions": decisions}, open(JUDGED, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(meta)


def cmd_score(a):
    CAND, JUDGED, OUT = paths(a.mode)
    qs = {q["id"]: q for q in json.load(open("eval/rag/questions_v3.json", encoding="utf-8"))}
    cand = {r["id"]: r for r in json.load(open(CAND, encoding="utf-8"))["rows"]}
    judged = json.load(open(JUDGED, encoding="utf-8")) if os.path.exists(JUDGED) else {"decisions": {}, "meta": {}}
    dec = judged["decisions"]
    # цитаты модели сверяются с корпусом заново: так сверка не зависит от того, где шёл шаг judge
    for qid, ds in dec.items():
        for h, d in zip(cand[qid]["candidates"], ds):
            if d.get("decision") == verify.APPLICABLE:
                text = verify.corpus_text(h)
                exact = verify.find_quote(d.get("model_quote", ""), text) if text else None
                d["quote"], d["verified"] = exact or "", bool(exact)

    def chosen(variant, r):
        if not r["eligible"]:
            return None
        if variant in ("old", "old_open"):
            return r[variant]
        if variant == "first":
            return r["candidates"][0] if r["candidates"] else None
        if variant == "default" and qs[r["id"]]["set"] == "правила" and r["old"]:
            # как verify.enrich_verified: у замечания правила документ известен, пункт без модели
            return r["old"]
        ds = dec.get(r["id"])
        if not ds:
            return None
        p = verify.pick({"candidates": r["candidates"]}, ds)
        return p[0] if p else None

    res, wrong = {}, {}
    variants = (("old", "прежний путь, без открытого поиска"), ("old_open", "прежний путь + открытый поиск"),
                ("first", "первый кандидат без модели"), ("verified", "проверка моделью"),
                ("default", "по умолчанию: правила с известным документом без модели, остальное проверка моделью"))
    for wide in (False, True):
        for variant, label in variants:
            label = label + (" | эталон с поправкой" if wide else "")
            res[label] = {}
            for set_name in ("правила", "правки"):
                c = {"с ответом": 0, "без ответа": 0, "приложен верный": 0, "приложен неверный": 0, "приложен к вопросу без ответа": 0,
                     "отказ с ответом": 0, "отказ без ответа": 0}
                for qid, q in qs.items():
                    if q["set"] != set_name:
                        continue
                    ev = chosen(variant, cand[qid])
                    gold = gold_wide(q) if wide else q["gold"]
                    if q["negative"]:
                        c["без ответа"] += 1
                        c["отказ без ответа" if not ev else "приложен к вопросу без ответа"] += 1
                        if ev:
                            wrong.setdefault(label, []).append([qid, ev["document"], ev["number"]])
                    else:
                        c["с ответом"] += 1
                        if not ev:
                            c["отказ с ответом"] += 1
                        elif any(match(ev, g) for g in gold):
                            c["приложен верный"] += 1
                        else:
                            c["приложен неверный"] += 1
                            wrong.setdefault(label, []).append([qid, ev["document"], ev["number"]])
                att = c["приложен верный"] + c["приложен неверный"] + c["приложен к вопросу без ответа"]
                c["точность приложенных"] = round(c["приложен верный"] / att, 3) if att else None
                c["покрытие"] = round(c["приложен верный"] / max(1, c["с ответом"]), 3)
                res[label][set_name] = c
    # решения модели по кандидатам: сколько эталонных пунктов модель отклонила
    gold_rejected, gold_seen, unverified = 0, 0, 0
    for qid, ds in dec.items():
        q = qs[qid]
        for h, d in zip(cand[qid]["candidates"], ds):
            if d.get("decision") == verify.APPLICABLE and not d.get("verified"):
                unverified += 1
            if not q["negative"] and any(match(h, g) for g in q["gold"]):
                gold_seen += 1
                gold_rejected += d.get("decision") != verify.APPLICABLE
    res["решения модели"] = {"эталонных пунктов среди кандидатов": gold_seen, "из них модель не признала применимыми": gold_rejected,
                             "«применим», но цитата не сверилась": unverified, **judged.get("meta", {})}
    json.dump({"metrics": res, "wrong": wrong}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("--mode", default="hybrid")
    c.add_argument("--device", default="cpu")
    c.add_argument("--k", type=int, default=5)
    for name in ("judge", "score"):
        sub.add_parser(name).add_argument("--mode", default="hybrid")
    sub.choices["judge"].add_argument("--missing", action="store_true", help="спросить только вопросы без решений")
    a = ap.parse_args()
    {"candidates": cmd_candidates, "judge": cmd_judge, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
