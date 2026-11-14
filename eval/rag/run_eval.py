"""Замер поиска норм: BM25, эмбеддинги (3 модели), гибрид RRF, гибрид + реранкер; recall@k, MRR, отказ.

Запуск (из корня репозитория):
  TZCHECK_REFDATA_OFFLINE=1 HF_HOME=data/rag-model-cache .venv/Scripts/python eval/rag/run_eval.py [--rerank] [--device cuda]
Вход: eval/rag/questions_v2.json (build_questions.py), индекс data/norm_corpus/index.json и векторы.
Выход: eval/rag/metrics_v2.json, eval/rag/rankings_v2.json.
"""
import argparse
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tzcheck.rag import Search, mentioned_documents, explicit_unknown, evidence_for, law_targets  # noqa: E402
from tzcheck.rag import models as M  # noqa: E402

K = 10
DEPTH = 100


def match(row, target):
    doc, num = target
    if row["document"] != doc:
        return False
    return num is None or row["number"] == num or row["number"].startswith(num + ",")


def first_rank(order, rows, gold, doc_level=False):
    for r, i in enumerate(order[:K], 1):
        if any(match(rows[i], (g[0], None) if doc_level else g) for g in gold):
            return r
    return None


def summarize(qs, ranks, gates, label):
    pos = [q for q in qs if not q["negative"]]
    neg = [q for q in qs if q["negative"]]
    out = {"method": label, "positives": len(pos), "negatives": len(neg)}
    for lvl in ("clause", "doc"):
        rr = [ranks[q["id"]][lvl] for q in pos]
        out[lvl] = {f"recall@{k}": round(sum(1 for r in rr if r and r <= k) / max(1, len(rr)), 3) for k in (1, 5, 10)}
        out[lvl]["mrr@10"] = round(sum(1 / r for r in rr if r) / max(1, len(rr)), 3)
    # Порог отказа: перебор по оценке лучшего фрагмента, максимум сбалансированной точности
    # (положительный вопрос «ответ дан и первый фрагмент верный» против отказа на вопросе без ответа).
    cands = sorted({round(gates[q["id"]], 4) for q in qs if gates[q["id"]] is not None})
    best = None
    for th in [-1e9] + cands:
        ans_ok = sum(1 for q in pos if gates[q["id"]] is not None and gates[q["id"]] >= th and ranks[q["id"]]["clause"] == 1)
        refused = sum(1 for q in neg if gates[q["id"]] is None or gates[q["id"]] < th)
        bal = 0.5 * ans_ok / max(1, len(pos)) + 0.5 * refused / max(1, len(neg))
        if best is None or bal > best[0]:
            answered = sum(1 for q in pos if gates[q["id"]] is not None and gates[q["id"]] >= th)
            best = (bal, th, ans_ok / max(1, len(pos)), refused / max(1, len(neg)), answered / max(1, len(pos)))
    out["refusal"] = {"threshold": best[1], "balanced": round(best[0], 3), "pos_top1_correct_and_answered": round(best[2], 3),
                      "neg_refused": round(best[3], 3), "pos_answered": round(best[4], 3)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--models", default=",".join(M.MODELS))
    args = ap.parse_args()
    qs = json.load(open("eval/rag/questions_v2.json", encoding="utf-8"))
    lex = Search()
    rows = lex.rows
    import numpy as np
    filt, refused_by_name = {}, set()
    for q in qs:
        docs = mentioned_documents(q["query"], rows)
        if docs:
            filt[q["id"]] = lex.allowed(docs)
        elif explicit_unknown(q["query"], rows):
            refused_by_name.add(q["id"])
            filt[q["id"]] = set()
        else:
            filt[q["id"]] = None
    rankings, timing = {}, {}

    def masked(scores_vec, allowed):
        if allowed is None:
            return scores_vec
        m = np.full(len(rows), -9.0, dtype="float32")
        if allowed:
            idx = np.fromiter(allowed, dtype=int)
            m[idx] = scores_vec[idx]
        return m

    t = time.time()
    bm = {}
    for q in qs:
        sc = lex.scores(q["query"], filt[q["id"]])
        order = sorted(sc, key=lambda i: -sc[i])[:DEPTH]
        bm[q["id"]] = (order, {i: sc[i] for i in order})
    timing["bm25"] = (time.time() - t) / len(qs)
    rankings["bm25"] = bm
    dense = {}
    for model in [m for m in args.models.split(",") if m]:
        try:
            vecs = np.asarray(M.load_vectors(model))
        except (OSError, ValueError) as e:
            print("нет векторов", model, e)
            continue
        enc = M.encoder(model, args.device)
        t = time.time()
        qv = enc.encode([M.prefix(model, True) + q["query"] for q in qs], batch_size=16, normalize_embeddings=True, show_progress_bar=False)
        res = {}
        for q, v in zip(qs, qv):
            s = masked(vecs @ v, filt[q["id"]])
            order = [int(i) for i in np.argsort(-s)[:DEPTH] if s[int(i)] > -9]
            res[q["id"]] = (order, {i: float(s[i]) for i in order})
        timing["dense:" + model] = (time.time() - t) / len(qs)
        dense[model] = res
        rankings["dense:" + model] = res
        hyb = {}
        for q in qs:
            d_order, d_sc = res[q["id"]]
            order = M.rrf([d_order, bm[q["id"]][0]])[:DEPTH]
            gate = {i: d_sc.get(i, float(vecs[i] @ qv[qs.index(q)])) for i in order[:1]}
            hyb[q["id"]] = (order, gate)
        rankings["hybrid:" + model] = hyb
        del enc
    if args.rerank and dense:
        best_model = max(dense, key=lambda m: sum(1 for q in qs if not q["negative"]
                                                  and first_rank(rankings["hybrid:" + m][q["id"]][0], rows, q["gold"]) == 1))
        ce = M.cross_encoder(args.device)
        t = time.time()
        rr = {}
        compute = 0.0
        for n, q in enumerate(qs):
            order = rankings["hybrid:" + best_model][q["id"]][0]
            pool = order[:20]
            if pool:
                t_q = time.time()
                vals = ce.predict([(q["query"], M.doc_text(rows[i])) for i in pool], batch_size=8, show_progress_bar=False, activation_fn=None)
                prob = {i: 1 / (1 + math.exp(-float(v))) for i, v in zip(pool, vals)}
                order = sorted(pool, key=lambda i: -prob[i]) + order[20:]
                rr[q["id"]] = (order, prob)
                compute += time.time() - t_q
                if args.device != "cpu":
                    time.sleep((time.time() - t_q) * 0.8)
            else:
                rr[q["id"]] = ([], {})
            if n % 50 == 0:
                print("rerank", n, f"{time.time() - t:.0f} c", flush=True)
        timing["rerank:" + best_model] = compute / len(qs)
        rankings["rerank:" + best_model] = rr
    metrics = {"questions": len(qs), "sets": {}, "seconds_per_query": {k: round(v, 3) for k, v in timing.items()}}
    for name, res in rankings.items():
        for set_name in ("все", "правки", "правила"):
            sub = [q for q in qs if set_name == "все" or q["set"] == set_name]
            ranks, gates = {}, {}
            for q in sub:
                order, sc = res[q["id"]]
                ranks[q["id"]] = {"clause": first_rank(order, rows, q["gold"]) if q["gold"] else None,
                                  "doc": first_rank(order, rows, q["gold"], True) if q["gold"] else None}
                gates[q["id"]] = sc.get(order[0]) if order and q["id"] not in refused_by_name else None
            metrics["sets"].setdefault(set_name, []).append(summarize(sub, ranks, gates, name))
    # Как в отчёте: evidence_for с norm_ids замечания (статья из базы норм, обозначение стандарта или поиск с отказом).
    prod = {"attached_correct": 0, "attached_wrong": 0, "refused_pos": 0, "refused_neg": 0, "attached_neg": 0}
    os.environ.setdefault("TZCHECK_RAG_MODE", "bm25")
    for q in qs:
        if q["set"] != "правила":
            continue
        ev = evidence_for(q["query"], q["norm_ids"], lex)
        if q["negative"]:
            prod["refused_neg" if not ev else "attached_neg"] += 1
        elif not ev:
            prod["refused_pos"] += 1
        else:
            ok = any(match(ev, g) for g in q["gold"])
            prod["attached_correct" if ok else "attached_wrong"] += 1
    metrics["production_bm25_rules"] = prod
    json.dump(metrics, open("eval/rag/metrics_v2.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    slim = {name: {qid: [[rows[i]["document"], rows[i]["number"]] for i in o[:5]] for qid, (o, _) in res.items()} for name, res in rankings.items()}
    json.dump(slim, open("eval/rag/rankings_v2.json", "w", encoding="utf-8"), ensure_ascii=False)
    for set_name, lst in metrics["sets"].items():
        print("==", set_name)
        for m in lst:
            c, d, r = m["clause"], m["doc"], m["refusal"]
            print(f"{m['method']:<34} R@1 {c['recall@1']:.3f} R@5 {c['recall@5']:.3f} R@10 {c['recall@10']:.3f} MRR {c['mrr@10']:.3f} | "
                  f"док R@1 {d['recall@1']:.3f} | отказ {r['neg_refused']:.2f} ответ {r['pos_answered']:.2f} порог {r['threshold']}")
    print(metrics["seconds_per_query"], metrics["production_bm25_rules"])


if __name__ == "__main__":
    main()
