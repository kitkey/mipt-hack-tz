"""Прогон с локальной моделью для demo/output/16-19: lift_src и lift_fin, оркестрации linear и graph.

Нужен OpenAI-совместимый сервер с Qwen3.8-27B (README, раздел «Локальная модель»). Без сервера скрипт завершается с ошибкой.
Запуск: python demo/run_llm_cases.py
"""
import json
import os
import sys
import time
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TZCHECK_REFDATA_OFFLINE", "1")
os.environ.setdefault("TZCHECK_LLM_MODEL", "openai/qwen3.8-27b")
os.environ.setdefault("TZCHECK_LLM_API_BASE", "http://127.0.0.1:8091/v1")
os.environ.setdefault("TZCHECK_LLM_API_KEY", "local")
os.environ.setdefault("TZCHECK_LLM_WORKERS", "1")
os.environ.setdefault("TZCHECK_LLM_TIMEOUT", "900")
os.environ.setdefault("TZCHECK_LLM_MAX_TOKENS", "6000")

from tzcheck.pipeline import check  # noqa: E402
from tzcheck.report import write_all  # noqa: E402

OUT = os.path.join(ROOT, "demo", "output")
CASES = [("16_lift_src_llm_linear", "lift_src", "linear"), ("17_lift_src_llm_graph", "lift_src", "graph"),
         ("18_lift_fin_llm_linear", "lift_fin", "linear"), ("19_lift_fin_llm_graph", "lift_fin", "graph")]


def main():
    urlopen(os.environ["TZCHECK_LLM_API_BASE"].rstrip("/") + "/models", timeout=10).read()
    for name, doc, orch in CASES:
        folder = os.path.join(OUT, name)
        src = os.path.join(ROOT, "samples", doc + ".docx")
        t = time.time()
        res = check(src, use_llm=True, orchestrator=orch)
        paths = write_all(res, src, folder)
        main_n = sum(f["section"] == "main" for f in res["findings"])
        print(json.dumps({"case": name, "status": res["status"], "seconds": round(time.time() - t), "main": main_n,
                          "from_model": res["stats"]["from_model"], "by_severity": res["stats"]["by_severity"], "model_block": res["stats"]["model_block"],
                          "steps": res["steps"], "warnings": [w for w in res["warnings"] if w.startswith("LLM")], "files": sorted(paths)},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
