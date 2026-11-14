"""PaddleOCR (lang=ru) на 116 картинках страниц: CER/WER против текстового слоя той же страницы PDF, числа, коды ОКПД 2,
время на страницу; затем сканы целиком через конвейер: совпадение замечаний со сканом и с DOCX.

Запуск (из корня, окружение с paddleocr, см. requirements-ocr.txt):
  TZCHECK_REFDATA_OFFLINE=1 python eval/ocr_compare/run_ocr.py [--limit N] [--skip-pages] [--skip-docs]
Выход: eval/ocr_compare/results_ocr.json, тексты в eval/ocr_compare/ocr_text/.
"""
import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(ROOT)
os.environ.setdefault("TZCHECK_REFDATA_OFFLINE", "1")
os.environ.setdefault("TZCHECK_OCR", "1")

import pymupdf  # noqa: E402
from benchmark_text import distance, findings as match_findings, dedupe_vmerge  # noqa: E402

OUT = Path("eval/ocr_compare/ocr_text")


def norm(s):
    s = s.casefold().replace("ё", "е").replace("|", " ")
    s = re.sub(r"[«»“”„\"]", '"', s)
    s = re.sub(r"[‐-―−]", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def words(s):
    return re.findall(r"\w+", norm(s))


def numbers(s):
    return re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?!\w)", s)


def codes(s):
    return re.findall(r"(?<!\d)\d{2}\.\d{2}\.\d{2}\.\d{3}(?!\d)", s)


def recall(ref, cand):
    x, y = collections.Counter(ref), collections.Counter(cand)
    return {"matched": sum((x & y).values()), "reference": sum(x.values())}


def page_metrics(ref_text, ocr_text):
    rt, ot = norm(ref_text), norm(ocr_text)
    rw, ow = words(ref_text), words(ocr_text)
    return {"cer": distance(rt, ot) / max(1, len(rt)), "wer": distance(rw, ow) / max(1, len(rw)),
            "words_bag": recall(rw, ow), "numbers": recall(numbers(norm(ref_text)), numbers(norm(ocr_text))),
            "okpd": recall(codes(ref_text), codes(ocr_text)), "ref_chars": len(rt)}


def run_pages(limit=None):
    from tzcheck import ocr
    t = time.perf_counter()
    ocr.ocr_array(json.load(open("eval/ocr_compare/generated/manifest.json", encoding="utf-8"))[0]["clean"].replace("\\", "/"))
    load = time.perf_counter() - t
    manifest = json.load(open("eval/ocr_compare/generated/manifest.json", encoding="utf-8"))
    pdfs = {}
    rows = []
    jobs = [(m, kind) for m in manifest for kind in ("clean", "degraded")]
    for n, (m, kind) in enumerate(jobs[:limit] if limit else jobs):
        pdf = pdfs.setdefault(m["pdf"], pymupdf.open(m["pdf"]))
        ref = pdf[m["page"] - 1].get_text("text", sort=True)
        img = m[kind].replace("\\", "/")
        t = time.perf_counter()
        lines = ocr.ocr_array(img)
        sec = time.perf_counter() - t
        text = "\n".join(lines)
        dst = OUT / m["name"] / str(m["dpi"]) / f"{m['page']:03d}_{kind}.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        row = {"name": m["name"], "page": m["page"], "dpi": m["dpi"], "kind": kind, "seconds": round(sec, 2), "lines": len(lines)}
        row.update(page_metrics(ref, text))
        rows.append(row)
        print(n + 1, len(jobs), m["name"], m["page"], m["dpi"], kind, f"{sec:.1f}s", f"CER {row['cer']:.3%}",
              f"words {row['words_bag']['matched']}/{row['words_bag']['reference']}", f"restarts {ocr.RESTARTS}", flush=True)
    return {"engine_load_and_first_page_s": round(load, 1), "worker_restarts": ocr.RESTARTS, "pages": rows}


def aggregate(rows):
    out = {}
    for key in sorted({(r["dpi"], r["kind"]) for r in rows}):
        sub = [r for r in rows if (r["dpi"], r["kind"]) == key]
        chars = sum(r["ref_chars"] for r in sub)
        agg = {"pages": len(sub),
               "cer_weighted": round(sum(r["cer"] * r["ref_chars"] for r in sub) / max(1, chars), 4),
               "wer_mean": round(sum(r["wer"] for r in sub) / len(sub), 4),
               "seconds_per_page_median": sorted(r["seconds"] for r in sub)[len(sub) // 2]}
        for f in ("words_bag", "numbers", "okpd"):
            m = sum(r[f]["matched"] for r in sub)
            n = sum(r[f]["reference"] for r in sub)
            agg[f + "_recall"] = round(m / n, 4) if n else None
        out[f"{key[0]}dpi_{key[1]}"] = agg
    return out


def scan_pdf(name, dpi=300, kind="clean"):
    """PDF без текстового слоя из картинок страниц (как скан)."""
    manifest = json.load(open("eval/ocr_compare/generated/manifest.json", encoding="utf-8"))
    pages = sorted((m for m in manifest if m["name"] == name and m["dpi"] == dpi), key=lambda m: m["page"])
    doc = pymupdf.open()
    for m in pages:
        pix = pymupdf.Pixmap(m[kind].replace(chr(92), "/"))
        page = doc.new_page(width=pix.width * 72 / dpi, height=pix.height * 72 / dpi)
        page.insert_image(page.rect, filename=m[kind].replace("\\", "/"))
    path = OUT / f"{name}_scan_{dpi}_{kind}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return str(path)


def run_docs(names):
    from tzcheck.parse import parse
    from tzcheck.pipeline import check
    out = []
    for name in names:
        for kind in ("clean", "degraded"):
            scan = scan_pdf(name, 300, kind)
            ref_docx = f"samples/{name}.docx"
            text_pdf = {"lift_src": "samples/pdf/lift_src.pdf", "metro_src": "samples/pdf/metro_src.pdf"}.get(name, f"data_raw/pdf_test/{name}.pdf")
            t = time.perf_counter()
            rs = check(scan, use_llm=False)
            sec = time.perf_counter() - t
            rd = check(ref_docx, use_llm=False)
            rp = check(text_pdf, use_llm=False)
            ref = parse(ref_docx)
            scan_text = "\n".join(b.text for b in rs["_doc"].blocks)
            ref_text = dedupe_vmerge(ref, ref_docx)
            row = {"name": name, "kind": kind, "pages": len(pymupdf.open(scan)), "seconds_total": round(sec, 1),
                   "cer_vs_docx": round(distance(norm(ref_text), norm(scan_text)) / len(norm(ref_text)), 4),
                   "numbers_vs_docx": recall(numbers(norm(ref_text)), numbers(norm(scan_text))),
                   "okpd_vs_docx": recall(codes(ref_text), codes(scan_text)),
                   "findings_scan_vs_docx": match_findings(rd["findings"], rs["findings"]),
                   "findings_scan_vs_textpdf": match_findings(rp["findings"], rs["findings"]),
                   "findings_textpdf_vs_docx": match_findings(rd["findings"], rp["findings"])}
            out.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    return out


def rescore(res):
    """Метрики по сохранённым текстам после исправления латинских двойников (tzcheck.ocr.fix_homoglyphs):
    исправление чисто текстовое, поэтому пересчёт по сохранённому выводу равен повторному прогону."""
    from tzcheck.ocr import fix_homoglyphs
    pdfs, rows = {}, []
    for r in res["pages"]:
        m = next(x for x in json.load(open("eval/ocr_compare/generated/manifest.json", encoding="utf-8"))
                 if x["name"] == r["name"] and x["page"] == r["page"] and x["dpi"] == r["dpi"])
        pdf = pdfs.setdefault(m["pdf"], pymupdf.open(m["pdf"]))
        ref = pdf[m["page"] - 1].get_text("text", sort=True)
        src = OUT / r["name"] / str(r["dpi"]) / f"{r['page']:03d}_{r['kind']}.txt"
        text = chr(10).join(fix_homoglyphs(x) for x in src.read_text(encoding="utf-8").splitlines())
        row = dict(r)
        row.update(page_metrics(ref, text))
        rows.append(row)
    return aggregate(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--skip-pages", action="store_true")
    ap.add_argument("--skip-docs", action="store_true")
    ap.add_argument("--out", default="eval/ocr_compare/results_ocr.json")
    ap.add_argument("--rescore", action="store_true")
    a = ap.parse_args()
    path = Path(a.out)
    res = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if a.rescore:
        res["summary_raw"] = aggregate(res["pages"])
        res["summary"] = rescore(res)
        path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({"raw": res["summary_raw"], "fixed": res["summary"]}, ensure_ascii=False, indent=1))
        return
    res["engine"] = {"name": "PaddleOCR PP-OCRv5", "det": os.environ.get("TZCHECK_OCR_DET", "mobile"),
                     "mkldnn": os.environ.get("TZCHECK_OCR_MKLDNN", "1"), "rec": "eslav_PP-OCRv5_mobile_rec",
                     "device": os.environ.get("TZCHECK_OCR_DEVICE", "cpu")}
    if not a.skip_pages:
        pages = run_pages(a.limit)
        res.update(pages)
        res["summary"] = aggregate(pages["pages"])
        path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(res["summary"], ensure_ascii=False, indent=1))
    if not a.skip_docs:
        res["documents"] = run_docs(["lift_src", "metro_src"])
        path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
