"""Прогон линейного и графового режима на четырёх образцах: число замечаний, совпадение, время, оценки уверенности.

  python eval/confidence/run_modes.py            # только правила
  python eval/confidence/run_modes.py --llm      # с моделью из TZCHECK_LLM_* (локальный сервер)
  python eval/confidence/run_modes.py --stub     # с заглушкой модели из tests/test_confidence.py (проверка переходов)
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("TZCHECK_REFDATA_OFFLINE", "1")

from tzcheck import llm
from tzcheck.graph import mermaid
from tzcheck.pipeline import check

DOCS = ["lift_src", "lift_fin", "metro_src", "metro_fin"]
HERE = os.path.dirname(os.path.abspath(__file__))


def key(f):
    return f["category"], f["block_id"], f["quote"]


def scores(r):
    return {"оценено моделью": sum(f["confidence_pct"] is not None for f in r["findings"]),
            "правило": sum(f["confidence_basis"] == "правило" for f in r["findings"]),
            "нет оценки": sum(f["confidence_pct"] is None and f["confidence_basis"] != "правило" for f in r["findings"]),
            "повторно проверено": sum("повторная проверка" in f["evidence"] for f in r["findings"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--docs", nargs="*", default=DOCS)
    args = ap.parse_args()
    use_llm = args.llm or args.stub
    if args.stub:
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from test_confidence import StubModel
        os.environ["TZCHECK_LLM_MODEL"] = "stub/model"
        llm._call = StubModel(verify_score=40)
    tag = "stub" if args.stub else ("llm" if args.llm else "rules")
    rows = []
    for name in args.docs:
        path = os.path.join(ROOT, "samples", name + ".docx")
        out = {"doc": name}
        res = {}
        check(path, use_llm=False)
        for mode in ("linear", "graph"):
            if args.stub:
                llm._call.seen.clear()
            t = time.time()
            r = check(path, use_llm=use_llm, orchestrator=mode)
            res[mode] = r
            out[mode] = {"findings": len(r["findings"]), "seconds": round(time.time() - t, 2), "steps": r["steps"], "confidence": scores(r)}
            print(f"  {name} {mode}: {len(r['findings'])} замечаний, {round(time.time() - t)} с", flush=True)
        a, b = ({key(f) for f in res[m]["findings"]} for m in ("linear", "graph"))
        out["same_findings"] = res["linear"]["findings"] == res["graph"]["findings"]
        out["only_linear"] = [list(k) for k in sorted(a - b)][:20]
        out["only_graph"] = [list(k) for k in sorted(b - a)][:20]
        rows.append(out)
        if args.llm:
            for m in ("linear", "graph"):
                out[m]["model"] = res[m].get("model")
                out[m]["llm_warnings"] = [w for w in res[m]["warnings"] if w.startswith("LLM")]
                out[m]["scores"] = sorted(f["confidence_pct"] for f in res[m]["findings"] if f["confidence_pct"] is not None)
            json.dump(res["graph"]["findings"], open(os.path.join(HERE, f"findings_graph_{name}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            json.dump(res["linear"]["findings"], open(os.path.join(HERE, f"findings_linear_{name}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump(rows, open(args.out or os.path.join(HERE, f"modes_{tag}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"{name}: linear {out['linear']['findings']} за {out['linear']['seconds']} с, graph {out['graph']['findings']} за "
              f"{out['graph']['seconds']} с, совпадают {out['same_findings']}, шаги графа {out['graph']['steps']}, "
              f"уверенность граф {out['graph']['confidence']}", flush=True)
    path = args.out or os.path.join(HERE, f"modes_{tag}.json")
    json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "graph.mmd"), "w", encoding="utf-8") as f:
        f.write(mermaid())


if __name__ == "__main__":
    main()
