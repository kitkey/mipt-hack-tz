"""Замер маскирования ПД: регулярки, NER отдельно и регулярки+NER на 30 ТЗ из ЕИС и проверка образцов.

python eval/pii/pii_eval.py --device cpu            # замер и проверка образцов, сводка в eval/pii/summary_cpu.json
python eval/pii/pii_eval.py --device cuda --timing-only   # только время: регулярки и регулярки+NER, summary_cuda.json

Разметка (настоящие значения) лежит вне репозитория: eval/pii/work/gold.json (в .gitignore).
Формат: {"<файл>": [{"kind": "PERSON", "value": "..."}, ...]}. В сводку попадают только агрегаты.
Без LibreOffice .doc заранее переводятся в eval/pii/work/converted/NN.docx (soffice в образе tzcheck).
"""
import argparse
import csv
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("TZCHECK_PII_NER_CACHE", os.path.join(ROOT, "data", "pii-model-cache"))

from tzcheck import pii
from tzcheck.parse import parse

HOLDOUT = os.path.join(ROOT, "data_raw", "eis_holdout")
WORK = os.path.join(ROOT, "eval", "pii", "work")
SAMPLES = ["lift_src.docx", "lift_fin.docx", "metro_src.docx", "metro_fin.docx", "injection.docx"]
KINDS = ["PERSON", "PHONE", "EMAIL", "SNILS", "PASSPORT", "INN", "OMS", "CARD", "DOC"]
ADDRESS = {"COUNTRY", "REGION", "DISTRICT", "CITY", "STREET", "HOUSE"}
KEY_PATTERNS = {
    "ГОСТ и другие стандарты": re.compile(r"(?:ГОСТ|СП|СНиП|ПНСТ|ТР ТС|ТР ЕАЭС)(?:\s?[РIЕСОИЭМК]{1,3})?\s?[\d.\-–/]{3,}"),
    "коды ОКПД 2 и КТРУ": re.compile(r"\b\d{2}\.\d{2}(?:\.\d{1,3}){0,2}(?:-\d{8})?\b"),
    "марки и модели (латиница)": re.compile(r"\b[A-Z][A-Za-z0-9\-]{2,}(?:\s[A-Z0-9][A-Za-z0-9\-]+)*"),
}


def blocks_of(path: str) -> list[str]:
    os.makedirs(os.path.join(WORK, "blocks"), exist_ok=True)
    cache = os.path.join(WORK, "blocks", os.path.basename(path) + ".json")
    if os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    conv = os.path.join(WORK, "converted", os.path.basename(path)[:2] + ".docx")
    doc = parse(conv if path.endswith(".doc") and os.path.exists(conv) else path)
    texts = [b.text for b in doc.blocks]
    json.dump(texts, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return texts


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


VARIANTS = ["regex", "ner", "regex+ner", "regex+ner без отбора"]


def scrub_doc(texts: list[str], variant: str) -> tuple[list[tuple[str, str]], float, list[str]]:
    saved = pii.PATTERNS, pii._plausible, pii.PERSON_CONTEXT, pii.NAMED_AFTER
    if variant == "ner":
        pii.PATTERNS = []
    if variant.endswith("без отбора"):
        pii._plausible, pii.PERSON_CONTEXT, pii.NAMED_AFTER = (lambda k, v: True), False, re.compile(r"(?!)")
    pii._cache.clear()
    scr = pii.Scrubber(ner=variant != "regex")
    t0 = time.perf_counter()
    try:
        scr.prefetch(texts)
        out = [scr.scrub(t) for t in texts]
    finally:
        pii.PATTERNS, pii._plausible, pii.PERSON_CONTEXT, pii.NAMED_AFTER = saved
    took = time.perf_counter() - t0
    assert all(scr.restore(o) == t for o, t in zip(out, texts)), "restore вернул не исходный текст"
    found = [(tok.strip("[]").rsplit("_", 1)[0], val) for tok, val in scr.mapping.items()]
    return found, took, out


def overlaps(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    return bool(a) and bool(b) and (a in b or b in a)


def mentions(value: str, texts: list[str]) -> int:
    rx = re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)", re.I)
    return sum(len(rx.findall(t)) for t in texts)


def score(pred: dict, outs: dict, texts: dict, gold: dict) -> dict:
    """Точность по уникальным замаскированным значениям документа, полнота по упоминаниям из разметки:
    упоминание пропущено, если после маскирования значение (фамилия, номер, адрес почты) осталось в тексте."""
    res = {}
    for kind in KINDS:
        p_all = tp = g_all = left = 0
        for doc in gold:
            p = [v for k, v in pred.get(doc, []) if k == kind]
            g = [x["value"] for x in gold[doc] if x["kind"] == kind]
            p_all += len(p)
            tp += sum(any(overlaps(v, x) for x in g) for v in p)
            for x in g:
                g_all += mentions(x, texts[doc])
                left += mentions(x, outs[doc])
        if p_all or g_all:
            res[kind] = {"найдено": p_all, "верно": tp, "ложно": p_all - tp, "упоминаний в разметке": g_all,
                         "замаскировано упоминаний": g_all - left, "пропущено упоминаний": left,
                         "точность": round(tp / p_all, 3) if p_all else None,
                         "полнота": round((g_all - left) / g_all, 3) if g_all else None}
    return res


def holdout(device: str, variants: list[str] = VARIANTS) -> dict:
    files = [r["файл"] for r in csv.DictReader(open(os.path.join(HOLDOUT, "manifest.csv"), encoding="utf-8-sig"), delimiter=";")]
    gold_path = os.path.join(WORK, "gold.json")
    gold = json.load(open(gold_path, encoding="utf-8")) if os.path.exists(gold_path) else {}
    preds = {v: {} for v in variants}
    outs = {v: {} for v in variants}
    all_texts = {}
    times = {"regex": [], "regex+ner": []}
    chars, failed = [], []
    for f in files:
        path = os.path.join(HOLDOUT, f)
        try:
            texts = blocks_of(path)
        except Exception as e:
            failed.append(f"{f}: {type(e).__name__}")
            continue
        chars.append(sum(map(len, texts)))
        all_texts[f] = texts
        for v in preds:
            found, took, out = scrub_doc(texts, v)
            preds[v][f] = found
            outs[v][f] = out
            if v in times:
                times[v].append(took)
        print(f"{f[:45]:45s} символов {chars[-1]:7d}  NER {times['regex+ner'][-1]:6.2f} с", flush=True)
    json.dump(preds, open(os.path.join(WORK, f"pred_{device}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    gold = {d: v for d, v in gold.items() if d in all_texts}
    only = {}
    for kind in KINDS if "ner" in preds else []:
        r = [(d, v) for d in preds["regex"] for k, v in preds["regex"][d] if k == kind]
        n = [(d, v) for d in preds["ner"] for k, v in preds["ner"][d] if k == kind]
        only_ner = sum(not any(d == d2 and overlaps(v, v2) for d2, v2 in r) for d, v in n)
        only_re = sum(not any(d == d2 and overlaps(v, v2) for d2, v2 in n) for d, v in r)
        if r or n:
            only[kind] = {"регулярки": len(r), "NER отдельно": len(n), "только NER": only_ner, "только регулярки": only_re}
    med = lambda xs: round(sorted(xs)[len(xs) // 2], 3) if xs else None
    return {
        "документов": len(chars), "не разобраны": failed, "размечено документов": len(gold),
        "символов на документ, медиана": med(chars),
        "время на документ, с": {"устройство": device, "регулярки медиана": med(times["regex"]),
                                 "регулярки+NER медиана": med(times["regex+ner"]),
                                 "регулярки+NER максимум": round(max(times["regex+ner"]), 2) if times["regex+ner"] else None,
                                 "регулярки+NER всего": round(sum(times["regex+ner"]), 1)},
        "пересечение регулярок и NER": only,
        "по типам": {v: score(preds[v], outs[v], all_texts, gold) for v in preds} if gold and "ner" in preds else None,
    }


def samples() -> dict:
    res = {}
    for name in SAMPLES:
        texts = blocks_of(os.path.join(ROOT, "samples", name))
        doc = parse(os.path.join(ROOT, "samples", name))
        fields = [v["text"] if isinstance(v, dict) else v for k, v in doc.fields.items() if isinstance(v, (str, dict))]
        row = {}
        for variant in ("regex", "regex+ner"):
            found, _, out = scrub_doc(texts + fields, variant)
            masked = {}
            for k, _v in found:
                masked[k] = masked.get(k, 0) + 1
            damaged, total = {}, {}
            for label, rx in KEY_PATTERNS.items():
                items = [(i, m.group(0)) for i, t in enumerate(texts) for m in rx.finditer(t)]
                total[label] = len(items)
                damaged[label] = sum(val not in out[i] for i, val in items)
            addr = [(i, texts[i][s:e]) for i, t in enumerate(texts) for k, s, e, _p in pii.ner_spans(t) if k in ADDRESS]
            total["адрес и место поставки (по разметке NER)"] = len(addr)
            damaged["адрес и место поставки (по разметке NER)"] = sum(val not in out[i] for i, val in addr)
            total["поля: заголовок, предмет, место"] = len(fields)
            damaged["поля: заголовок, предмет, место"] = sum(o != f for o, f in zip(out[len(texts):], fields))
            row[variant] = {"замаскировано": masked, "ключевых мест": total, "из них задето метками": damaged}
        res[name] = row
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--no-samples", action="store_true")
    ap.add_argument("--timing-only", action="store_true")
    args = ap.parse_args()
    os.environ["TZCHECK_PII_NER_DEVICE"] = args.device
    t0 = time.perf_counter()
    if pii._load() is None:
        sys.exit("модель NER не загрузилась")
    out = {"модель": pii.NER_MODEL, "загрузка модели, с": round(time.perf_counter() - t0, 1), "порог": pii._min_score()}
    import torch
    out["потоков torch"] = torch.get_num_threads()
    out["ЕИС"] = holdout(args.device, ["regex", "regex+ner"] if args.timing_only else VARIANTS)
    if not (args.no_samples or args.timing_only):
        out["образцы"] = samples()
    path = os.path.join(ROOT, "eval", "pii", f"summary_{args.device}{'_timing' if args.timing_only else ''}.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
