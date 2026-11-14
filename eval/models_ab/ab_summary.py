"""Сводка прогонов run_eval.py: python eval/models_ab/ab_summary.py rules qwen gemma"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, suffix=""):
    p = HERE / f"{name}{suffix}.json"
    return json.loads(p.read_text(encoding="utf8")) if p.is_file() else None


def summary(name):
    res, docs, calls = load(name), load(name, "_documents") or [], load(name, "_calls") or []
    out = {"name": name}
    if res:
        t = sum(res["total"].values())
        out["place"] = f"{sum(res['hit'].values())}/{t}"
        out["category"] = f"{sum(res['hit_cat'].values())}/{t}"
        out["strict"] = f"{sum(res['strict'].values())}/{t}"
        out["classes"] = {c: f"{res['hit'].get(c, 0)}/{res['hit_cat'].get(c, 0)}/{res['strict'].get(c, 0)} из {n}"
                          for c, n in sorted(res["total"].items(), key=lambda x: -x[1])}
    out["docs"] = []
    for d in docs:
        fs = d.get("findings") or []
        llm = [f for f in fs if str(f.get("source", "")).startswith("llm")]
        warn = d.get("warnings") or []
        out["docs"].append({"file": Path(str(d.get("file", ""))).name, "mode": d.get("mode"), "seconds": d.get("seconds"),
                            "findings": len(fs), "high": sum(f.get("severity") == "high" for f in fs),
                            "llm_added": len(llm), "llm_added_high": sum(f.get("severity") == "high" for f in llm),
                            "rules_verified_by_llm": sum("+llm" in str(f.get("source", "")) for f in fs),
                            "llm_skipped": sum("фрагмент пропущен" in w for w in warn),
                            "not_json": sum("не JSON" in w for w in warn),
                            "cut": sum("обрезан" in w for w in warn),
                            "timeouts": sum("Timeout" in w for w in warn)})
    if calls:
        ok = [c for c in calls if "error" not in c]
        sec = sum(c["seconds"] for c in ok)
        comp = sum(c.get("completion_tokens") or 0 for c in ok)
        out["calls"] = {"n": len(calls), "errors": len(calls) - len(ok),
                        "error_types": sorted({c["error"] for c in calls if "error" in c}),
                        "length_cut": sum(c.get("finish_reason") == "length" for c in ok),
                        "prompt_tokens_mean": round(sum(c.get("prompt_tokens") or 0 for c in ok) / max(len(ok), 1)),
                        "completion_tokens_mean": round(comp / max(len(ok), 1)),
                        "seconds_total": round(sec, 1), "seconds_mean": round(sec / max(len(ok), 1), 1),
                        "completion_tok_per_s_end_to_end": round(comp / sec, 1) if sec else None}
    return out


if __name__ == "__main__":
    rows = [summary(n) for n in sys.argv[1:]]
    print(json.dumps(rows, ensure_ascii=False, indent=1))
