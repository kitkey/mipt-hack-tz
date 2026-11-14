"""Собирает demo/output: отчёты системы на примерах кейса и на ТЗ из ЕИС, проверка моделью (справочники без сети).

Нужна модель: TZCHECK_LLM_MODEL и ключ (README, «Быстрый старт»); без неё скрипт останавливается. Каждый документ
проходит все шаги ядра, вызовы платные. Все входы открытые (образцы кейса и ТЗ, опубликованные в ЕИС), поэтому
документы помечаются открытыми (public, TZCHECK_DOC_PUBLIC=1) и внешняя модель вызывается без слоя NER.
Отчёты по документам ЕИС проходят маскирование ПД (контакты сотрудников заказчиков), DOCX с комментариями
для них не сохраняется, потому что это копия исходного файла.
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TZCHECK_REFDATA_OFFLINE", "1")
os.environ.setdefault("TZCHECK_FEEDBACK", os.path.join(tempfile.gettempdir(), "tzcheck_demo_feedback.jsonl"))
os.environ.setdefault("TZCHECK_DOC_PUBLIC", "1")  # сравнение версий флага public не принимает

from tzcheck import llm
from tzcheck.checks import succeeded
from tzcheck.pii import Scrubber
from tzcheck.pipeline import check, compare
from tzcheck.report import to_compare_html, to_html, write_all

OUT = os.path.join(ROOT, "demo", "output")
EIS = os.path.join(ROOT, "data_raw", "eis_more")
SPLIT = os.path.join(ROOT, "eval", "reconcile", "split", "02")

CASES = [
    ("01_metro_src", [os.path.join(ROOT, "samples", "metro_src.docx")], False),
    ("02_lift_src", [os.path.join(ROOT, "samples", "lift_src.docx")], False),
    ("03_lift_src_pdf", [os.path.join(ROOT, "samples", "pdf", "lift_src.pdf")], False),
    ("04_lift_fin", [os.path.join(ROOT, "samples", "lift_fin.docx")], False),
    ("05_metro_fin", [os.path.join(ROOT, "samples", "metro_fin.docx")], False),
    ("07_eis_server_msu", [os.path.join(EIS, "07_МГУ_сервер.docx")], True),
    ("08_eis_repair_mtusi", [os.path.join(EIS, "08_МТУСИ_ремонт_помещений.docx")], True),
    ("09_eis_cleaning", [os.path.join(EIS, "eis_32616557989.docx")], True),
    ("10_eis_medical_volggmu", [os.path.join(EIS, "12_ВолгГМУ_нейродиагностика.docx")], True),
    ("11_eis_lift_with_appendices", [os.path.join(SPLIT, "main.docx")] + sorted(
        os.path.join(SPLIT, f) for f in os.listdir(SPLIT) if f != "main.docx") if os.path.isdir(SPLIT) else [], True),
]


def _mask(text: str, scr: Scrubber) -> str:
    return scr.scrub(text)


def _short(res: dict) -> dict:
    sections = {}
    for f in res.get("findings", []):
        sections[f["section"]] = sections.get(f["section"], 0) + 1
    return {"file": res.get("file"), "status": res["status"], "mode": res.get("mode"), "model": res.get("model"), "seconds": res.get("seconds"),
            "findings": len(res.get("findings", [])), "sections": sections, "by_severity": res.get("stats", {}).get("by_severity", {}),
            "by_category": res.get("stats", {}).get("by_category", {}), "needs_review": res.get("stats", {}).get("needs_review"),
            "warnings": len(res.get("warnings", [])), "pii_in_document": res.get("pii_masked", {})}


def run_case(name, paths, mask):
    folder = os.path.join(OUT, name)
    if not paths or not all(os.path.exists(p) for p in paths):
        print(f"{name}: входных файлов нет в репозитории, оставлен готовый отчёт")
        return {"case": name, "status": "skipped", "reason": "корпус ЕИС не входит в репозиторий: в нём контакты сотрудников заказчиков"}
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    res = check(paths if len(paths) > 1 else paths[0], use_llm=True, public=True)
    if not succeeded(res):
        with open(os.path.join(folder, "result.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        return {"case": name, **_short(res)}
    scr = Scrubber()
    scr.scrub(res["_doc"].text())
    res["pii_masked"] = scr.summary()
    base = os.path.splitext(os.path.basename(paths[0]))[0]
    if mask:
        scr = Scrubber()
        data = {k: v for k, v in res.items() if not k.startswith("_")}
        data["files_note"] = "ПД в отчёте заменены метками [PERSON_n], [PHONE_n], [EMAIL_n]"
        with open(os.path.join(folder, "report.json"), "w", encoding="utf-8") as f:
            f.write(_mask(json.dumps(data, ensure_ascii=False, indent=2), scr))
        with open(os.path.join(folder, "report.html"), "w", encoding="utf-8") as f:
            f.write(_mask(to_html(res), scr))
    else:
        write_all(res, paths[0], folder)
        for f in os.listdir(folder):
            os.replace(os.path.join(folder, f), os.path.join(folder, f.replace(base + ".", "")))
    return {"case": name, "inputs": [os.path.relpath(p, ROOT).replace("\\", "/") for p in paths], **_short(res)}


def run_rag():
    """Тот же lift_src с пунктами норм из корпуса (TZCHECK_RAG=1, BM25: работает и без torch)."""
    saved = {k: os.environ.get(k) for k in ("TZCHECK_RAG", "TZCHECK_RAG_MODE")}
    os.environ.update(TZCHECK_RAG="1", TZCHECK_RAG_MODE="bm25")
    try:
        out = run_case("13_lift_src_rag", [os.path.join(ROOT, "samples", "lift_src.docx")], False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    out["with_norm_quote"] = sum(1 for f in json.load(open(os.path.join(OUT, "13_lift_src_rag", "report.json"), encoding="utf-8"))["findings"]
                                 if "пункт нормы" in f["evidence"])
    return out


def run_compare():
    folder = os.path.join(OUT, "06_compare_metro")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    cmp = compare(os.path.join(ROOT, "samples", "metro_src.docx"), os.path.join(ROOT, "samples", "metro_fin.docx"), use_llm=True)
    with open(os.path.join(folder, "compare.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in cmp.items() if not k.startswith("_")}, f, ensure_ascii=False, indent=2)
    if cmp["status"] != "ok":
        # сравнение не собрано (одна из проверок не дошла до результата): причина в compare.json
        return {"case": "06_compare_metro", "status": cmp["status"], "old": cmp.get("old"), "new": cmp.get("new")}
    with open(os.path.join(folder, "compare.html"), "w", encoding="utf-8") as f:
        f.write(to_compare_html(cmp))
    return {"case": "06_compare_metro", "status": cmp["status"], "summary": cmp["summary"],
            "fixed": len(cmp["fixed"]), "remained": len(cmp["remained"]), "added": len(cmp["added"])}


def run_broken():
    folder = os.path.join(OUT, "12_broken_files")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    tmp = tempfile.mkdtemp()
    cases = {"empty.docx": b"", "not_a_docx.docx": b"plain text with docx extension", "truncated.pdf": b"%PDF-1.7\n1 0 obj\n"}
    out = []
    for fname, content in cases.items():
        p = os.path.join(tmp, fname)
        with open(p, "wb") as f:
            f.write(content)
        res = check(p, use_llm=True, public=True)
        out.append({"file": fname, "status": res["status"], "error": res.get("error"), "summary": res.get("summary")})
    with open(os.path.join(folder, "results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"case": "12_broken_files", "results": out}


def main():
    if not llm.enabled():
        sys.exit("Модель не задана (TZCHECK_LLM_MODEL): без неё содержательной проверки нет, отчёты не собираются.")
    os.makedirs(OUT, exist_ok=True)
    summary = [run_case(*c) for c in CASES[:5]] + [run_compare()] + [run_case(*c) for c in CASES[5:]] + [run_broken(), run_rag(), run_case("15_injection", [os.path.join(ROOT, "samples", "injection.docx")], False)]
    scan = os.path.join(OUT, "14_lift_scan_ocr", "report.json")
    if os.path.exists(scan):
        # скан собирается в образе с WITH_OCR=1: curl -F file=@samples/scan/lift_src_scan.pdf .../api/check
        summary.append({"case": "14_lift_scan_ocr", "inputs": ["samples/scan/lift_src_scan.pdf"], "built_in": "docker WITH_OCR=1",
                        **_short(json.load(open(scan, encoding="utf-8")))})
    path = os.path.join(OUT, "summary.json")
    if any(s.get("status") == "skipped" for s in summary) and os.path.exists(path):
        old = {s["case"]: s for s in json.load(open(path, encoding="utf-8"))}
        summary = [old.get(s["case"], s) if s.get("status") == "skipped" else s for s in summary]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    for s in summary:
        print(json.dumps(s, ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
