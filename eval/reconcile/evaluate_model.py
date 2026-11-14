"""Run semantic-matching evaluation only when authorized service is available."""
import json
import os
from pathlib import Path
import sys
import tempfile
import docx
import requests
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["TZCHECK_REFDATA_OFFLINE"] = "1"
from tzcheck.parse import parse_many
from tzcheck.pii import Scrubber
from tzcheck.reconcile import reconcile_semantic

ROOT = Path(__file__).resolve().parents[2]
DOCS = Path(os.environ.get("TZCHECK_EVAL_DOCS", ROOT / "samples"))
API_BASE = os.environ.get("TZCHECK_LLM_API_BASE", "http://127.0.0.1:8091/v1")
CASES = [("Номинальная масса: 10 кг", "Вес изделия: 20 кг"),
         ("Ширина кабины: 1 м", "Размер кабины по ширине: 2 м"),
         ("Грузоподъемность: 500 кг", "Масса поднимаемого груза: 1000 кг")]

def configure():
    """Только локальный OpenAI-совместимый сервер (Strata или llama.cpp); внешние API не вызываются."""
    try:
        response = requests.get(API_BASE.rstrip("/") + "/models", timeout=3)
        response.raise_for_status()
        models = response.json().get("data", [])
    except (requests.RequestException, ValueError):
        models = []
    if not models:
        raise RuntimeError(f"Локальный сервер модели недоступен: {API_BASE}")
    os.environ["TZCHECK_LLM_MODEL"] = "openai/" + models[0]["id"].removeprefix("openai/")
    os.environ["TZCHECK_LLM_API_BASE"] = API_BASE
    os.environ.setdefault("TZCHECK_LLM_API_KEY", "local")
    return "local"

def main():
    backend = configure()
    os.environ["TZCHECK_LLM_MAX_TOKENS"] = "1500"
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        sources = sorted(DOCS.glob("*.docx"))[:3]
        for source, (a,b) in zip(sources, CASES):
            d = docx.Document(source)
            for text in (b, a):
                paragraph = d.add_paragraph(text)
                d.element.body.insert(0, paragraph._p)
            path = Path(tmp) / source.name; d.save(path)
            document = parse_many([path]); found = reconcile_semantic(document, Scrubber())
            error = [w for w in document.warnings if "Семантическая сверка:" in w]
            rows.append({"source": source.name, "a": a, "b": b,
                         "found": any(a == f.quote and b == f.evidence.get("вторая цитата") for f in found),
                         "error": error, "other_candidates": len(found)})
    report = {"backend": backend, "model": os.environ["TZCHECK_LLM_MODEL"], "rows": rows}
    Path(__file__).with_name("model_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
