import os
import re
import time
import unicodedata
from collections import Counter

from . import confidence, feedback, llm
from .model import SECTION_LLM, SECTION_MAIN, STATUS_REVIEW, Finding, from_model
from .norms import NORMS
from .parse import ParseError, ParsedDoc, parse, parse_many
from .reconcile import build_graph, reconcile, reconcile_semantic
from .pii import Scrubber
from .rules import run_rules

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
HIDDEN_RE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
INJECTION_RE = re.compile(
    r"(?:ignore|disregard)\s+(?:all\s+|the\s+)?(?:previous|above)\s+instructions|игнорируй\w*\s+(?:все\s+)?(?:предыдущ|инструкц)|"
    r"ты\s+(?:теперь|должен)\s+(?:ответить|написать|считать)|system\s*prompt|не\s+сообщай\s+об\s+ошибк|ответь,?\s+что\s+ошибок\s+нет", re.I)


def _overlap(a: Finding, b: Finding) -> bool:
    if a.block_id != b.block_id:
        return False
    qa, qb = a.quote.lower(), b.quote.lower()
    return qa in qb or qb in qa or (a.category == b.category and len(set(qa.split()) & set(qb.split())) >= 3)


def _merge(rule: list[Finding], extra: list[Finding]) -> list[Finding]:
    out = list(rule)
    for f in extra:
        dup = next((x for x in out if _overlap(x, f)), None)
        if dup is None:
            out.append(f)
        elif "llm" in f.source and dup.source.startswith("rule") and dup.category == f.category:
            dup.evidence["подтверждено моделью"] = f.explanation[:300]
            if f.confidence_pct is not None and dup.confidence_pct is None:
                dup.confidence_pct, dup.confidence_basis = f.confidence_pct, f.confidence_basis
            if len(f.fix) > len(dup.fix):
                dup.fix = f.fix
    return out


def _places(n: int) -> str:
    word = "месте" if n % 10 == 1 and n % 100 != 11 else "местах"
    return f"{n} {word}"


def _remarks(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} замечание"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} замечания"
    return f"{n} замечаний"


def _verdict(findings) -> str:
    high = [f.id for f in findings if f.severity == "high"]
    errors = [f.id for f in findings if f.status == "ошибка"]
    review = sum(1 for f in findings if f.needs_review)
    if high:
        ids = ", ".join(high[:5]) + ("…" if len(high) > 5 else "")
        return f"Публиковать без исправлений нельзя: замечаний высокого риска {len(high)} ({ids})."
    if errors:
        return f"Перед публикацией исправить замечания со статусом «ошибка»: {len(errors)}."
    if findings:
        return f"Ошибок не найдено, мест на ручную проверку: {review or len(findings)}."
    return ""


def _collapse_repeats(findings: list[Finding], doc: ParsedDoc) -> list[Finding]:
    groups: dict = {}
    out = []
    for f in findings:
        key = (f.category, " ".join(f.quote.split())[:100].lower())
        first = groups.get(key)
        if first is None:
            groups[key] = f
            out.append(f)
        elif f.block_id != first.block_id and f.block_id not in first.also_in:
            first.also_in.append(f.block_id)
    for f in out:
        if f.also_in and "То же ещё в" not in f.explanation:
            locs = [b.location() for b in (doc.block(x) for x in f.also_in[:5]) if b]
            f.explanation += f" То же ещё в {_places(len(f.also_in))}: " + "; ".join(locs) + ("…" if len(f.also_in) > 5 else "") + "."
    return out


def _security_scan(doc: ParsedDoc) -> list[str]:
    warns = []
    hidden = [b for b in doc.blocks if HIDDEN_RE.search(b.text)]
    for b in doc.blocks:
        b.section = HIDDEN_RE.sub("", b.section or "")
    if hidden:
        n = len(hidden)
        where = "блоке" if n % 10 == 1 and n % 100 != 11 else "блоках"
        warns.append(f"Скрытые символы Unicode (нулевой ширины или смены направления текста) в {n} {where}: "
                     + ", ".join(b.location() for b in hidden[:5]) + ". Текст мог быть изменён незаметно для читателя.")
        for b in hidden:
            b.text = HIDDEN_RE.sub("", b.text)
            b.cells = [HIDDEN_RE.sub("", c) for c in b.cells]
    inj = [b for b in doc.blocks if INJECTION_RE.search(b.text)]
    if inj:
        warns.append("В документе есть фрагменты, похожие на инструкции для ИИ: " + ", ".join(b.location() for b in inj[:5])
                     + ". Модель обрабатывает текст ТЗ только как данные, но эти места стоит проверить вручную.")
    for b in doc.blocks:
        b.text = unicodedata.normalize("NFC", b.text)
    return warns


REFDATA_TITLES = {"okpd2": "ОКПД 2", "gost": "статусы ГОСТ и СП", "arshin": "типы СИ «Аршин»", "eaeu": "перечни ТР ЕАЭС", "laws": "редакции законов"}


def _passport(paths, model: str) -> dict:
    """Паспорт проверки: на каких версиях справочников, каком файле и какой модели получен отчёт."""
    import hashlib
    import json
    from . import refdata
    refs = {}
    for key, title in REFDATA_TITLES.items():
        try:
            with open(os.path.join(refdata.data_dir(), key + "_meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        refs[title] = {"версия": meta.get("source_version") or "", "получено": (meta.get("fetched_at") or "")[:10], "источник": meta.get("via") or meta.get("source_type") or ""}
    files = []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                files.append({"файл": os.path.basename(os.fspath(p)), "sha256": hashlib.sha256(fh.read()).hexdigest()})
        except OSError:
            pass
    return {"справочники": refs, "файлы": files, "модель": model or "не подключена",
            "справочники офлайн": refdata.offline(), "пункты норм": os.environ.get("TZCHECK_RAG", "") in ("1", "true", "yes", "on")}


ORCHESTRATORS = ("linear", "graph")


def orchestrator_name(name: str | None = None) -> str:
    name = (name or os.environ.get("TZCHECK_ORCHESTRATOR") or "linear").strip().lower()
    if name not in ORCHESTRATORS:
        raise ValueError(f"orchestrator must be one of {ORCHESTRATORS}")
    return name


def check(path: str, use_llm: bool | None = None, orchestrator: str | None = None) -> dict:
    if orchestrator_name(orchestrator) == "graph":
        from .graph import check_graph
        return check_graph(path, use_llm)
    t0 = time.time()
    try:
        paths = [path] if isinstance(path, (str, os.PathLike)) else list(path)
        doc = parse_many(paths)
    except ParseError as e:
        return {"file": os.path.basename(os.fspath(path if isinstance(path, (str, os.PathLike)) else path[0])), "status": "error", "error": str(e), "findings": [], "summary": f"Документ не проверен: {e}"}
    security = _security_scan(doc)
    doc.warnings.extend(security)
    scr = Scrubber()
    findings = _collapse_repeats(run_rules(doc), doc)
    mode = "rules"
    if use_llm is None:
        use_llm = llm.enabled()
    if use_llm:
        mode = "rules+llm"
        doc.llm_deadline = time.time() + llm.doc_budget()
        findings = llm.verify_rules(doc, findings, scr)
        found = llm.analyze(doc, findings, scr) + llm.cross_check(doc, scr)
        findings = _merge(findings, llm.verify_new(doc, found, scr))
        findings = _collapse_repeats(findings, doc)
    findings.extend(reconcile(doc))
    if use_llm:
        findings.extend(reconcile_semantic(doc, scr))
    steps = ["parse", "security", "rules"] + (["llm_verify_rules", "llm_find", "llm_verify_new", "reconcile", "reconcile_llm"] if use_llm else ["reconcile"])
    return _finish(doc, paths, findings, scr, use_llm, mode, t0, "linear", steps)


def _finish(doc, paths, findings, scr, use_llm, mode, t0, orchestrator, steps) -> dict:
    findings = feedback.apply(findings)
    if os.environ.get("TZCHECK_RAG", "").lower() in ("1", "true", "yes", "on"):
        from .rag import enrich
        enrich(findings)
    confidence.apply(findings)
    for f in findings:
        if from_model(f.source):
            # замечания модели не смешиваются с правилами: отдельный блок, всегда ручная проверка, в вердикт не входят
            f.section, f.needs_review, f.status = SECTION_LLM, True, STATUS_REVIEW
            f.evidence["блок отчёта"] = "замечание модели, проверить вручную"
        f.finalize()
        if f.norm_ids:
            f.evidence["нормы"] = [f"{NORMS[n]['title']} (редакция: {NORMS[n].get('edition', 'см. источник')})" for n in f.norm_ids if n in NORMS]
    findings.sort(key=lambda f: (f.section == SECTION_LLM, SEV_ORDER.get(f.severity, 1), _order(doc, f.block_id)))
    main = [f for f in findings if f.section == SECTION_MAIN]
    extra = [f for f in findings if f.section == SECTION_LLM]
    for i, f in enumerate(main, 1):
        f.id = f"F{i:03d}"
    for i, f in enumerate(extra, 1):
        f.id = f"M{i:03d}"
    # счётчики, вердикт и заключение только по основному списку; у блока модели свои счётчики
    cats, sev = Counter(f.category for f in main), Counter(f.severity for f in main)
    statuses = Counter(f.status for f in main)
    review = sum(1 for f in main if f.needs_review)
    model_block = {"count": len(extra), "by_severity": dict(Counter(f.severity for f in extra)),
                   "by_category": dict(Counter(f.category for f in extra))}
    # заключение всегда собирает код из основного списка, текст модели идёт подписанной строкой в блок модели
    summary = _template_summary(main, cats, sev, review)
    verdict = _verdict(main)
    if verdict:
        summary = verdict + " " + summary
    if extra:
        summary += f" Отдельным блоком {_remarks(len(extra))} модели на ручную проверку, в вердикт они не входят."
    if use_llm and main:
        stats = (f"всего {len(main)}: высокий риск {sev.get('high', 0)}, средний {sev.get('medium', 0)}, низкий {sev.get('low', 0)}; "
                 f"требуют ручной проверки {review}. По категориям: " + "; ".join(f"{k} {v}" for k, v in cats.most_common()))
        note = llm.summarize(doc, main, stats, scr)
        if note:
            model_block["note"] = note
    return {
        "file": doc.name, "status": "ok", "mode": mode, "orchestrator": orchestrator, "steps": steps, "seconds": round(time.time() - t0, 1),
        "model": os.environ.get("TZCHECK_LLM_MODEL", "") if use_llm else "",
        "document": {"format": doc.source_format, "files": [os.path.basename(os.fspath(p)) for p in paths], "blocks": len(doc.blocks), "fields": doc.fields, "requirement_graph": build_graph(doc).to_dict()},
        "warnings": doc.warnings, "pii_masked": scr.summary(),
        "stats": {"by_category": dict(cats), "by_severity": dict(sev), "by_status": dict(statuses), "needs_review": review,
                  "confidence": {"оценено моделью": sum(f.confidence_pct is not None for f in findings),
                                 "правило": sum(f.confidence_basis == confidence.RULE for f in findings),
                                 "нет оценки": sum(f.confidence_pct is None and f.confidence_basis != confidence.RULE for f in findings),
                                 "порог повторной проверки": confidence.low_threshold()},
                  "from_model": len(extra), "model_block": model_block},
        "summary": summary, "findings": [f.to_dict() for f in findings],
        "norms": {k: NORMS[k] for k in sorted({n for f in findings for n in f.norm_ids})},
        "passport": _passport(paths, os.environ.get("TZCHECK_LLM_MODEL", "") if use_llm else ""),
        "_doc": doc, "_paths": [os.fspath(p) for p in paths],
    }


def _order(doc, bid):
    for i, b in enumerate(doc.blocks):
        if b.id == bid:
            return i
    return 10 ** 6


def _template_summary(findings, cats, sev, review) -> str:
    if not findings:
        return "Явных проблем не найдено. Рекомендуется выборочная ручная проверка характеристик и сроков."
    top = ", ".join(f"«{k}» ({v})" for k, v in cats.most_common(3))
    first = "В первую очередь исправить замечания с высоким риском; на" if sev.get("high") else "На"
    return (f"Найдено {_remarks(len(findings))}, из них с высоким риском {sev.get('high', 0)}. Чаще всего: {top}. "
            f"{first} ручную проверку по внешним источникам {_remarks(review)} (реестр СИ, действие стандартов, коды ОКПД 2).")


def _fkey(f: dict) -> tuple:
    return f["category"], " ".join(f["quote"].lower().split())[:80]


def compare(old_path: str, new_path: str, use_llm: bool | None = None, orchestrator: str | None = None) -> dict:
    old, new = check(old_path, use_llm, orchestrator), check(new_path, use_llm, orchestrator)
    if old["status"] != "ok" or new["status"] != "ok":
        return {"status": "error", "old": old.get("error"), "new": new.get("error")}
    ok = {_fkey(f): f for f in old["findings"]}
    nk = {_fkey(f): f for f in new["findings"]}
    fixed = [f for k, f in ok.items() if k not in nk]
    remained = [f for k, f in nk.items() if k in ok]
    added = [f for k, f in nk.items() if k not in ok]
    return {"status": "ok", "old": old["file"], "new": new["file"],
            "fixed": fixed, "remained": remained, "added": added,
            "summary": f"Исправлено {len(fixed)}, осталось {len(remained)}, новых {len(added)}.",
            "_old": old, "_new": new}
