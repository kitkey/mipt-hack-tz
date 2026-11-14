import os
import re
import time
import unicodedata
from collections import Counter

from . import confidence, core, feedback, gost_check, llm, passport, replacements, tables
from . import progress as prog
from .checks import categories_for, group_names, group_stats, is_full, only_groups, parse_checks, partial_note, verdict_code
from .model import SECTION_LLM, SECTION_MAIN, SECTION_UNCHECKED, STATUS_REVIEW, Finding, from_model
from .norms import NORMS, apply_editions
from .parse import ParseError, ParsedDoc, parse, parse_many
from .pii import Scrubber

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
HIDDEN_RE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
INJECTION_RE = re.compile(
    r"(?:ignore|disregard)\s+(?:all\s+|the\s+)?(?:previous|above)\s+instructions|игнорируй\w*\s+(?:все\s+)?(?:предыдущ|инструкц)|"
    r"ты\s+(?:теперь|должен)\s+(?:ответить|написать|считать)|system\s*prompt|не\s+сообщай\s+об\s+ошибк|ответь,?\s+что\s+ошибок\s+нет", re.I)


def _places(n: int) -> str:
    word = "месте" if n % 10 == 1 and n % 100 != 11 else "местах"
    return f"{n} {word}"


def _remarks(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} замечание"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} замечания"
    return f"{n} замечаний"


def _ids(ids: list) -> str:
    return ", ".join(ids[:5]) + ("…" if len(ids) > 5 else "")


def _verdict(findings) -> str:
    """Публикацию блокирует только высокий риск со статусом «ошибка»; высокий риск на ручной проверке идёт отдельной фразой."""
    high = [f.id for f in findings if f.severity == "high" and f.status == "ошибка"]
    high_review = [f.id for f in findings if f.severity == "high" and f.status != "ошибка"]
    errors = [f.id for f in findings if f.status == "ошибка"]
    review = sum(1 for f in findings if f.needs_review)
    tail = f" Ещё высокого риска на ручную проверку {len(high_review)} ({_ids(high_review)})." if high_review else ""
    if high:
        return f"Публиковать без исправлений нельзя: ошибок высокого риска {len(high)} ({_ids(high)})." + tail
    if high_review:
        return f"Подтверждённых ошибок высокого риска нет; до публикации проверить вручную замечания высокого риска: {len(high_review)} ({_ids(high_review)})."
    if errors:
        return f"Перед публикацией исправить замечания со статусом «ошибка»: {len(errors)}."
    if findings:
        return f"Ошибок не найдено, мест на ручную проверку: {review or len(findings)}."
    return ""


def _collapse_repeats(findings: list[Finding], doc: ParsedDoc) -> list[Finding]:
    groups: dict = {}
    out = []
    for f in findings:
        if not f.quote.strip():
            # замечание на документ в целом: без цитаты не с чем сливать
            out.append(f)
            continue
        key = (f.category, " ".join(f.quote.split())[:100].lower())
        first = groups.get(key)
        if first is None:
            groups[key] = f
            out.append(f)
        elif f.block_id != first.block_id and f.block_id not in first.also_in:
            first.also_in.append(f.block_id)
            first.trace.append({"step": "repeats", "text": f"та же цитата ещё в блоке {f.block_id}: замечания объединены"})
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


def check(path: str, use_llm: bool | None = None, orchestrator: str | None = None, checks=None, progress=None) -> dict:
    """checks: группы проверок (logic, standards, risks; None = все); progress: callable(event) для хода по шагам (tzcheck.progress)."""
    groups = parse_checks(checks)
    if orchestrator_name(orchestrator) == "graph":
        from .graph import check_graph
        return check_graph(path, use_llm, checks=groups, progress=progress)
    t0 = time.time()
    if use_llm is None:
        use_llm = llm.enabled()
    rep = prog.reporter(progress)
    rep.plan(prog.plan_steps("linear", use_llm, groups))
    rep.start("parse")
    try:
        paths = [path] if isinstance(path, (str, os.PathLike)) else list(path)
        doc = parse_many(paths)
    except ParseError as e:
        rep.fail("parse", str(e))
        return {"file": os.path.basename(os.fspath(path if isinstance(path, (str, os.PathLike)) else path[0])), "status": "error", "error": str(e), "findings": [], "summary": f"Документ не проверен: {e}"}
    rep.done("parse", blocks=len(doc.blocks), files=len(paths))
    prog.attach(doc, rep)
    doc.check_categories = categories_for(groups)
    rep.start("security")
    security = _security_scan(doc)
    doc.warnings.extend(security)
    rep.done("security", warnings=len(security))
    scr = Scrubber()
    if use_llm:
        findings, steps = run_core(doc, scr, groups, rep)
        mode = "model"
    else:
        findings, steps, mode = [], [], "format"
    steps = ["parse", "security"] + steps
    return _finish(doc, paths, findings, scr, use_llm, mode, t0, "linear", steps, groups=groups, rep=rep)


NO_MODEL = "Модель не настроена: проверено только оформление и безопасность (разбор файлов, скрытые символы, инструкции для ИИ). Содержательной проверки требований не было."


def start_core(doc):
    doc.llm_deadline = time.time() + llm.doc_budget()
    doc.journal = llm.Journal()
    doc.model_calls, doc.model_dropped, doc.llm_rejected = {}, [], []


def core_passport(doc, scr, rep):
    rep.start("passport")
    p = passport.detect(doc, scr)
    rep.done("passport", law=p.get("закон"), customer=p.get("заказчик"))
    rep.start("tables")
    m = tables.build(doc, scr)
    rep.done("tables", tables=sum(t.marked for t in m.tables), positions=len(m.positions))


def core_find(doc, scr, groups, rep) -> list[Finding]:
    cats = categories_for(groups)
    rep.start("core_find")
    found = core.find(doc, scr, cats)
    rep.update(stage="справочные записи по обозначениям, которые назвала модель")
    found += core.refs(doc, scr, cats)
    rep.update(stage="сверка позиций с чеклистами ГОСТ")
    found += gost_check.check(doc, scr, cats)
    rep.update(stage="проверка «не указано» по всему комплекту")
    found = core.absent(doc, scr, found)
    rep.done("core_find", found=len(found))
    return found


def core_cross(doc, scr, groups, rep) -> list[Finding]:
    rep.start("core_cross")
    found = core.cross(doc, scr, categories_for(groups))
    rep.done("core_cross", found=len(found))
    return found


def core_verify(doc, scr, groups, found, rep) -> list[Finding]:
    found = only_groups(core.dedupe(found), groups)
    rep.start("core_verify", candidates=len(found))
    kept = core.verify(doc, scr, found)
    rep.done("core_verify", confirmed=len(kept))
    return _collapse_repeats(kept, doc)


def run_core(doc, scr, groups, rep) -> tuple[list[Finding], list[str]]:
    """Ядро проверки: паспорт и роли, разметка таблиц, поиск дефектов, сверка частей, вторая проверка. Решения принимает модель."""
    start_core(doc)
    core_passport(doc, scr, rep)
    found = core_find(doc, scr, groups, rep)
    steps = ["passport", "tables", "core_find"]
    if "logic" in groups:
        found += core_cross(doc, scr, groups, rep)
        steps.append("core_cross")
    return core_verify(doc, scr, groups, found, rep), steps + ["core_verify"]


def _finish(doc, paths, findings, scr, use_llm, mode, t0, orchestrator, steps, groups=None, rep=None) -> dict:
    groups = parse_checks(groups)
    rep = prog.reporter(rep)
    rep.start("finish")
    findings = only_groups(findings, groups)
    findings = feedback.apply(findings)
    apply_editions()  # редакции законов из локальной копии на момент проверки: обновление подхватывается без перезапуска
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
        if f.section == SECTION_UNCHECKED:
            # вывод из отсутствия сведений в переданных файлах: вопрос закупщику, в вердикт и счётчики не входит
            f.needs_review, f.status = True, STATUS_REVIEW
            f.evidence["блок отчёта"] = "не проверено и не найдено в переданных файлах"
    findings.sort(key=lambda f: ({SECTION_MAIN: 0, SECTION_LLM: 1}.get(f.section, 2), SEV_ORDER.get(f.severity, 1), _order(doc, f.block_id)))
    main = [f for f in findings if f.section == SECTION_MAIN]
    extra = [f for f in findings if f.section == SECTION_LLM]
    unchecked = [f for f in findings if f.section == SECTION_UNCHECKED]
    for i, f in enumerate(main, 1):
        f.id = f"F{i:03d}"
    for i, f in enumerate(extra, 1):
        f.id = f"M{i:03d}"
    for i, f in enumerate(unchecked, 1):
        f.id = f"U{i:03d}"
    replacements.apply(findings)
    sev_ru = {"high": "высокий", "medium": "средний", "low": "низкий"}
    for f in findings:
        f.trace.append({"step": "final", "text": f"статус «{f.status}», риск {sev_ru.get(f.severity, f.severity)}"
                        + (", на ручную проверку" if f.needs_review else "") + (f", правка «{f.replacement}»" if f.replacement else "")})
    # счётчики, вердикт и заключение только по основному списку; у блока модели свои счётчики
    cats, sev = Counter(f.category for f in main), Counter(f.severity for f in main)
    statuses = Counter(f.status for f in main)
    review = sum(1 for f in main if f.needs_review)
    model_block = {"count": len(extra), "by_severity": dict(Counter(f.severity for f in extra)),
                   "by_category": dict(Counter(f.category for f in extra)), "by_group": dict(Counter(f.group for f in extra))}
    # заключение всегда собирает код из основного списка, текст модели идёт подписанной строкой в блок модели
    summary = _template_summary(main, cats, sev, review)
    by_group = Counter(f.group for f in main)
    if main:
        summary += " По группам: " + ", ".join(f"{g} {by_group.get(g, 0)}" for g in group_names(groups)) + "."
    verdict = _verdict(main)
    if verdict:
        summary = verdict + " " + summary
    partial = partial_note(groups)
    if partial:
        summary = partial + " " + summary
    if not use_llm:
        summary = NO_MODEL
    if unchecked:
        summary += f" Отдельным блоком «Не проверено»: {_remarks(len(unchecked))} о сведениях, которых нет в переданных файлах; в вердикт они не входят."
    if extra:
        summary += f" Отдельным блоком {_remarks(len(extra))} модели на ручную проверку, в вердикт они не входят."
    rejected = getattr(doc, "llm_rejected", [])
    journal = getattr(doc, "journal", None)
    if journal is not None:
        # снятые проверкой: в основной список не идут, в журнале лежат с цепочкой «модель предложила, проверка сняла»
        journal.rejected = []
        for i, f in enumerate(rejected, 1):
            f.id = f"R{i:03d}"
            journal.rejected.append(f.to_dict())
    if rejected:
        doc.warnings.append(f"LLM: вторая проверка сняла находок модели {len(rejected)}; в отчёт они не вошли (stats.model_rejected).")
    if use_llm and main:
        stats = (f"всего {len(main)}: высокий риск {sev.get('high', 0)}, средний {sev.get('medium', 0)}, низкий {sev.get('low', 0)}; "
                 f"требуют ручной проверки {review}. По категориям: " + "; ".join(f"{k} {v}" for k, v in cats.most_common()))
        with llm.journal_scope(getattr(doc, "journal", None), "summary", "Заключение по отчёту",
                               {"kind": "document", "title": doc.name, "blocks": []}):
            note = llm.summarize(doc, main, stats, scr)
        if note:
            model_block["note"] = note
    res = {
        "file": doc.name, "status": "ok", "mode": mode, "orchestrator": orchestrator, "steps": steps, "seconds": round(time.time() - t0, 1),
        "model": os.environ.get("TZCHECK_LLM_MODEL", "") if use_llm else "",
        "document": {"format": doc.source_format, "files": [os.path.basename(os.fspath(p)) for p in paths], "blocks": len(doc.blocks), "fields": doc.fields,
                     "procurement": getattr(doc, "passport", None) or {}, "passport_source": getattr(doc, "passport_source", ""),
                     "roles": dict(Counter(b.role or "не определена" for b in doc.blocks)) if use_llm else {},
                     "tables": tables.of(doc).to_dict() if use_llm else {}},
        "model_calls": getattr(doc, "model_calls", {}), "model_dropped": getattr(doc, "model_dropped", []),
        "trace_calls": len(journal.calls) if journal else 0, "trace_rejected": len(journal.rejected) if journal else 0,
        "_trace": journal.to_dict() if journal else {"calls": [], "rejected": [], "dropped": []},
        "warnings": doc.warnings, "pii_masked": scr.summary(),
        "stats": {"by_category": dict(cats), "by_severity": dict(sev), "by_status": dict(statuses), "needs_review": review,
                  "confidence": {"оценено моделью": sum(f.confidence_pct is not None for f in findings),
                                 "правило": sum(f.confidence_basis == confidence.RULE for f in findings),
                                 "нет оценки": sum(f.confidence_pct is None and f.confidence_basis != confidence.RULE for f in findings),
                                 "порог повторной проверки": confidence.low_threshold()},
                  "from_model": len(extra), "model_block": model_block,
                  "unchecked_block": {"count": len(unchecked), "by_rule": dict(Counter(f.rule_id for f in unchecked))},
                  "model_rejected": {"count": len(rejected), "by_category": dict(Counter(f.category for f in rejected))}},
        "summary": summary, "findings": [f.to_dict() for f in findings],
        "norms": {k: NORMS[k] for k in sorted({n for f in findings for n in f.norm_ids})},
        "passport": _passport(paths, os.environ.get("TZCHECK_LLM_MODEL", "") if use_llm else ""),
        "_doc": doc, "_paths": [os.fspath(p) for p in paths],
    }
    # группы проверок интерфейса (Логика, Стандарты, Риски): что проверялось и сводка по группам
    res["checks"] = list(groups)
    res["check_groups"] = group_names(groups)
    res["full_check"] = is_full(groups)
    res["verdict"] = (partial + " " if partial else "") + verdict
    res["verdict_code"] = verdict_code(main, groups)
    res["stats"]["by_group"] = {g: by_group.get(g, 0) for g in group_names(groups)}
    res["stats"]["groups"] = group_stats(main, groups)
    res["stats"]["revisions"] = sum(1 for f in main if f.replacement)
    rep.done("finish", findings=len(main), model=len(extra), unchecked=len(unchecked))
    rep.close()
    return res


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
