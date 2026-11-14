"""Ядро проверки на заглушке модели: решения о дефектах принимает модель, код раскладывает таблицы по её разметке,
привязывает цитату дословно и отдаёт модели кандидатов поиска."""
import json
import sys

import pytest
from conftest import fake_litellm

from tzcheck import core, llm, passport, pipeline, tables
from tzcheck.parse import Block, ParsedDoc
from tzcheck.pii import Scrubber

CATS = list(core.CATEGORIES)


def _row(bid, table, row, cells, vmerge=None, source="", section="Спецификация", parent="", nested=0):
    vm = vmerge or [False] * len(cells)
    shown = [c for c, v in zip(cells, vm) if not v]
    return Block(bid, "row", " | ".join(x for x in shown if x), section, table, row, list(cells), source=source, grid=list(cells),
                 hmerge=[False] * len(cells), vmerge=vm, parent=parent, nested=nested)


def _para(bid, text, section="Условия поставки", source=""):
    return Block(bid, "paragraph", text, section, source=source)


def spec_doc():
    blocks = [
        Block("h1", "heading", "Техническое задание на поставку", "Техническое задание на поставку"),
        _para("p1", "Поставка осуществляется в течение 30 календарных дней с даты заключения договора.", "Условия поставки"),
        _para("p2", "Гарантийный срок не менее 12 месяцев с даты подписания акта.", "Гарантия"),
        _row("t1r1", 0, 0, ["№", "Наименование", "Характеристика", "Значение", "Ед. изм.", "Кол-во"]),
        _row("t1r2", 0, 1, ["1", "Стол рабочий", "Длина", "1200", "мм", "4"]),
        _row("t1r3", 0, 2, ["1", "Стол рабочий", "Высота", "", "мм", "4"], vmerge=[True, True, False, False, False, True]),
        _row("t1r4", 0, 3, ["2", "Стул", "Высота сиденья", "450", "мм", "8"]),
        _para("c1", "Количество столов по договору: 5 штук.", "Проект договора", source="договор.docx"),
    ]
    return ParsedDoc("tz.docx", blocks, {}, [], "docx")


HEADER = {"header_rows": 1, "kind": "positions", "item": "",
          "columns": [{"index": 0, "role": "num", "label": "№"}, {"index": 1, "role": "name", "label": "Наименование"},
                      {"index": 2, "role": "char_name", "label": "Характеристика"}, {"index": 3, "role": "value", "label": "Значение"},
                      {"index": 4, "role": "char_unit", "label": "Ед. изм."}, {"index": 5, "role": "qty", "label": "Кол-во"}]}


class Stub:
    """Заглушка модели: ответ по началу задачи в запросе, запросы сохраняются."""

    def __init__(self, answers):
        self.answers, self.prompts = answers, []

    def __call__(self, prompt, max_tokens=4000):
        self.prompts.append(prompt)
        for key, ans in self.answers.items():
            if prompt.startswith("Задача: " + key):
                return ans(prompt) if callable(ans) else json.loads(json.dumps(ans))
        return {}

    def asked(self, key):
        return [p for p in self.prompts if p.startswith("Задача: " + key)]


@pytest.fixture
def stub(monkeypatch):
    def make(answers):
        s = Stub(answers)
        monkeypatch.setitem(sys.modules, "litellm", fake_litellm(s))
        monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
        return s
    return make


PASS = {"passport": {"law": "223-ФЗ", "method": "запрос котировок", "competitive": True, "kind": "товар", "subject": "поставка мебели",
                     "customer": "ООО Ромашка", "customer_profile": ""},
        "roles": [{"id": "h1", "role": "req"}, {"id": "p1", "role": "terms"}, {"id": "p2", "role": "warranty"},
                  {"id": "t1r1", "role": "spec"}, {"id": "c1", "role": "contract"}]}


def test_roles_and_passport_come_from_model(stub):
    s = stub({"паспорт": PASS})
    doc = spec_doc()
    p = passport.detect(doc, Scrubber())
    assert p["закон"] == "223-ФЗ" and p["конкурентная"] is True
    assert [doc.block(x).role for x in ("p1", "p2", "t1r3", "c1")] == ["terms", "warranty", "spec", "contract"]
    assert len(s.asked("паспорт")) == 1


def test_no_answer_leaves_roles_empty_and_checks_everything(stub):
    stub({})
    doc = spec_doc()
    passport.detect(doc, Scrubber())
    assert all(b.role == "" for b in doc.blocks)
    assert all(passport.checked(b) for b in doc.blocks)


def test_customer_profile_only_from_list(stub):
    stub({"паспорт": dict(PASS, passport=dict(PASS["passport"], customer_profile="unknown"))})
    doc = spec_doc()
    assert passport.detect(doc, Scrubber())["профиль заказчика"] == ""
    assert passport.practice(doc) == ""


def test_customer_profile_forced_or_switched_off_by_setting(stub, monkeypatch):
    """TZCHECK_CUSTOMER задаёт профиль заказчика, которого модель не узнала, и выключает узнанный; практика идёт в запрос поиска."""
    s = stub({"паспорт": PASS, "разметка шапки": HEADER, "найти дефекты": {"findings": [], "lookups": []}})
    monkeypatch.setenv("TZCHECK_CUSTOMER", "mipt")
    doc = spec_doc()
    assert passport.detect(doc, Scrubber())["профиль заказчика"] == "mipt"
    tables.build(doc, Scrubber())
    core.find(doc, Scrubber(), CATS)
    assert all("Практика закупщиков заказчика (МФТИ)" in p for p in s.asked("найти дефекты"))
    stub({"паспорт": dict(PASS, passport=dict(PASS["passport"], customer_profile="mipt"))})
    monkeypatch.setenv("TZCHECK_CUSTOMER", "none")
    doc = spec_doc()
    assert passport.detect(doc, Scrubber())["профиль заказчика"] == "" and passport.practice(doc) == ""
    monkeypatch.setenv("TZCHECK_CUSTOMER", "unknown")
    assert passport.detect(spec_doc(), Scrubber())["профиль заказчика"] == ""


def test_header_markup_one_call_per_table_and_layout(stub):
    s = stub({"паспорт": PASS, "разметка шапки": HEADER})
    doc = spec_doc()
    passport.detect(doc, Scrubber())
    m = tables.build(doc, Scrubber())
    assert len(s.asked("разметка шапки")) == 1
    assert [(p.name, p.qty) for p in m.positions] == [("Стол рабочий", "4"), ("Стул", "8")]
    assert [(c.name, c.value, c.unit) for c in m.positions[0].chars] == [("Длина", "1200", "мм"), ("Высота", "", "мм")]
    assert m.rows["t1r1"].header and m.rows["t1r3"].position is m.positions[0]


def test_table_without_markup_is_not_laid_out(stub):
    stub({"паспорт": PASS})
    doc = spec_doc()
    passport.detect(doc, Scrubber())
    m = tables.build(doc, Scrubber())
    assert m.positions == [] and not m.tables[0].marked


def test_nested_characteristics_go_to_outer_position(stub):
    blocks = [_row("t1r1", 0, 0, ["Наименование", "Характеристики"]), _row("t1r2", 0, 1, ["Шкаф", ""]),
              _row("t1r2n1r1", 0, 0, ["Ширина", "800 мм"], parent="t1r2", nested=1),
              _row("t1r2n1r2", 0, 1, ["Глубина", "400 мм"], parent="t1r2", nested=1)]
    doc = ParsedDoc("n.docx", blocks, {}, [], "docx")

    def head(prompt):
        if "Ширина" in prompt:
            return {"header_rows": 0, "kind": "characteristics", "columns": [{"index": 0, "role": "char_name"}, {"index": 1, "role": "value"}]}
        return {"header_rows": 1, "kind": "positions", "columns": [{"index": 0, "role": "name"}, {"index": 1, "role": "desc"}]}
    stub({"разметка шапки": head})
    m = tables.build(doc, Scrubber())
    assert len(m.positions) == 1 and [c.name for c in m.positions[0].chars] == ["Ширина", "Глубина"]
    assert tables.position(doc, "t1r2n1r2") is m.positions[0]


def test_whole_position_goes_to_model_not_fixed_chunk(stub):
    s = stub({"паспорт": PASS, "разметка шапки": HEADER, "найти дефекты": {"findings": [], "lookups": []}})
    doc = spec_doc()
    passport.detect(doc, Scrubber())
    tables.build(doc, Scrubber())
    core.find(doc, Scrubber(), CATS)
    prompts = s.asked("найти дефекты")
    pos = next(p for p in prompts if "Стол рабочий" in p)
    assert "[t1r2]" in pos and "[t1r3]" in pos and "Характеристика: Высота | Значение: (пусто)" in pos
    # общие условия поставки и гарантии даются вместе с позицией, договор в проверяемые части не идёт
    assert "[p1]" in pos and "[p2]" in pos
    assert not any("[c1]" in p.split("<<<ТЗ>>>")[-1] for p in prompts)


def _finding(**kw):
    base = {"block_id": "t1r3", "quote": "Высота", "category": "Неполнота или комплектность", "severity": "high",
            "explanation": "Не указано значение высоты стола.", "fix": "Указать высоту.", "norm_ids": [], "confidence": 80}
    base.update(kw)
    return base


def _prep(stub, answers):
    s = stub(dict({"паспорт": PASS, "разметка шапки": HEADER}, **answers))
    doc = spec_doc()
    pipeline.start_core(doc)
    passport.detect(doc, Scrubber())
    tables.build(doc, Scrubber())
    return s, doc


def test_quote_not_in_text_is_dropped(stub):
    s, doc = _prep(stub, {"найти дефекты": {"findings": [_finding(quote="высота столешницы не указана")]}})
    assert core.find(doc, Scrubber(), CATS) == []
    assert doc.model_dropped and doc.model_dropped[0]["почему"] == "цитаты нет в тексте"


def test_quote_is_bound_verbatim_to_block(stub):
    s, doc = _prep(stub, {"найти дефекты": {"findings": [_finding(block_id="t1r3", quote="стол  РАБОЧИЙ | длина")]}})
    out = core.find(doc, Scrubber(), CATS)
    assert len(out) == 1 and out[0].block_id == "t1r2" and out[0].quote == "Стол рабочий | Длина"


def test_unknown_category_is_dropped(stub):
    s, doc = _prep(stub, {"найти дефекты": {"findings": [_finding(category="Прочее")]}})
    assert core.find(doc, Scrubber(), CATS) == []


def test_absent_claim_checked_against_whole_set(stub):
    claim = _finding(block_id="t1r4", quote="Высота сиденья", explanation="Не указан гарантийный срок на стул.", absent=True, missing="гарантийный срок")

    def decide(prompt):
        assert "[p2]" in prompt  # кандидат поиска по комплекту
        return {"decisions": [{"n": 1, "decision": "same", "block_id": "p2", "reason": "гарантия 12 месяцев в общих условиях"}]}
    s, doc = _prep(stub, {"найти дефекты": lambda p: {"findings": [claim] if "Стул" in p else []}, "проверить утверждения": decide})
    found = core.absent(doc, Scrubber(), core.find(doc, Scrubber(), CATS))
    assert found == [] and len(doc.llm_rejected) == 1


def test_absent_claim_stated_elsewhere_is_rejected(stub):
    """«Не указано», которое нашлось в другой части комплекта, неверно: замечание снимается и остаётся в журнале снятых."""
    claim = _finding(block_id="t1r4", quote="Высота сиденья", explanation="Не указан гарантийный срок.", absent=True, missing="гарантийный срок")
    s, doc = _prep(stub, {"найти дефекты": lambda p: {"findings": [claim] if "Стул" in p else []},
                          "проверить утверждения": {"decisions": [{"n": 1, "decision": "other", "block_id": "p2", "reason": "есть"}]}})
    found = core.absent(doc, Scrubber(), core.find(doc, Scrubber(), CATS))
    assert found == [] and len(doc.llm_rejected) == 1
    assert doc.llm_rejected[0].evidence["проверка «не указано»"].startswith("указано в другой части комплекта: ")
    assert "снято: указано в другой части комплекта" in doc.llm_rejected[0].trace[-1]["text"]


def test_absent_claim_needs_shown_candidate_to_be_rejected(stub):
    """Модель сослалась на место, которого ей не показывали: утверждение не снято, замечание идёт на ручную проверку."""
    claim = _finding(block_id="t1r4", quote="Высота сиденья", explanation="Не указан гарантийный срок.", absent=True, missing="гарантийный срок")
    s, doc = _prep(stub, {"найти дефекты": lambda p: {"findings": [claim] if "Стул" in p else []},
                          "проверить утверждения": {"decisions": [{"n": 1, "decision": "same", "block_id": "t1r4", "reason": "есть"}]}})
    found = core.absent(doc, Scrubber(), core.find(doc, Scrubber(), CATS))
    assert len(found) == 1 and found[0].needs_review and doc.llm_rejected == []
    assert found[0].evidence["проверка «не указано»"].startswith("не подтверждена")


def test_absent_claim_confirmed_stays(stub):
    claim = _finding(absent=True, missing="значение высоты")
    s, doc = _prep(stub, {"найти дефекты": lambda p: {"findings": [claim] if "Стол" in p else []},
                          "проверить утверждения": {"decisions": [{"n": 1, "decision": "not_found", "reason": "нет"}]}})
    found = core.absent(doc, Scrubber(), core.find(doc, Scrubber(), CATS))
    assert len(found) == 1 and found[0].severity == "high"


def test_verify_shows_part_once_with_places_from_whole_set(stub):
    """Замечания одной позиции проверяются одним вызовом: позиция в запросе один раз, к доводу подобраны места комплекта."""
    items = [_finding(), _finding(block_id="t1r2", quote="Длина", severity="low", category="Гарантийные условия",
                                  explanation="Не указан гарантийный срок на стол.", norm_ids=["223-FZ-4-10-1"])]
    seen = []

    def verify(prompt):
        seen.append(prompt)
        return {"decisions": [{"n": 0, "decision": "keep", "reason": "значения нет", "confidence": 90},
                              {"n": 1, "decision": "reject", "reason": "гарантия 12 месяцев в [p2]", "confidence": 5}]}
    s, doc = _prep(stub, {"найти дефекты": lambda p: {"findings": items if "Стол" in p else []}, "независимая вторая проверка": verify})
    out = core.verify(doc, Scrubber(), core.find(doc, Scrubber(), CATS))
    assert len(seen) == 1 and seen[0].count("Характеристика: Длина") == 1
    data = seen[0].split("<<<ТЗ>>>")[-1]
    assert "Другие места комплекта:" in data and "[p2] (гарантия;" in data and "Гарантийный срок не менее 12 месяцев" in data
    assert "Другие места по этому замечанию: " in data and "Нормы из замечаний:" in data and "223-ФЗ, ст. 4, ч. 10, п. 1" in data
    assert [f.quote for f in out] == ["Высота"] and out[0].evidence["вторая проверка"] == "подтверждено"
    assert doc.llm_rejected[0].evidence["довод проверки"] == "гарантия 12 месяцев в [p2]"


def test_verify_sees_what_the_finding_stands_on(stub):
    """Вторая проверка видит справочные записи и второе место, на которых стоит довод, а не только сам довод."""
    def verify(prompt):
        assert "Справочные записи: ОКПД 2 «31.01.12» для позиции «Стол рабочий»: в классификаторе «Мебель деревянная для офисов»" in prompt
        assert "Второе место [c1] (проект договора): Количество столов по договору: 5 штук." in prompt
        return {"decisions": [{"n": 0, "decision": "lower", "reason": "нужен классификатор", "confidence": 40},
                              {"n": 1, "decision": "keep", "reason": "4 и 5 штук", "confidence": 95}]}
    s, doc = _prep(stub, {"найти дефекты": lambda p: {"findings": [_finding(), _finding(block_id="t1r2", quote="Длина", category="Противоречие")]
                                                   if "Стол" in p else []}, "независимая вторая проверка": verify})
    found = core.find(doc, Scrubber(), CATS)
    found[0]._records = ["ОКПД 2 «31.01.12» для позиции «Стол рабочий»: в классификаторе «Мебель деревянная для офисов»"]
    found[1].evidence.update({"второй блок": "c1", "вторая цитата": "5 штук"})
    out = core.verify(doc, Scrubber(), found)
    assert [(f.needs_review, f.evidence["вторая проверка"]) for f in out] == [(True, "понижено: на ручную проверку"), (False, "подтверждено")]


def test_verify_can_reject_or_lower_but_not_raise(stub):
    items = [_finding(), _finding(block_id="t1r4", quote="Высота сиденья", severity="medium", category="Противоречие"),
             _finding(block_id="t1r2", quote="Длина", severity="low", category="Стандарты (ГОСТ, поверка)")]

    def verify(prompt):
        assert "Характеристика: Длина" in prompt  # вся позиция в контексте второй проверки
        return {"decisions": [{"n": 0, "decision": "reject", "reason": "x"}, {"n": 1, "decision": "lower", "reason": "y"},
                              {"n": 2, "decision": "keep", "reason": "z", "confidence": 95}]}
    s, doc = _prep(stub, {"найти дефекты": lambda p: {"findings": items if "Стол" in p else []}, "независимая вторая проверка": verify})
    found = core.find(doc, Scrubber(), CATS)
    out = core.verify(doc, Scrubber(), found)
    sev = {f.quote: f.severity for f in out}
    assert "Высота" not in sev and sev["Высота сиденья"] == "low" and sev["Длина"] == "low"
    assert len(doc.llm_rejected) == 1


def test_cross_needs_both_quotes_verbatim(stub):
    good = {"block_id": "t1r2", "quote": "мм | 4", "other_block_id": "c1", "other_quote": "5 штук", "category": "Противоречие",
            "severity": "high", "explanation": "Количество столов 4 в спецификации и 5 в договоре.", "fix": "Согласовать."}
    bad = dict(good, other_quote="10 штук")
    s, doc = _prep(stub, {"найти дефекты": {"findings": []}, "сверка частей": {"findings": [good, bad]}})
    core.find(doc, Scrubber(), CATS)
    out = core.cross(doc, Scrubber(), CATS)
    assert len(out) == 1 and out[0].also_in == ["c1"] and out[0].evidence["вторая цитата"] == "5 штук"


def test_reference_records_only_for_named_designations(stub, monkeypatch):
    from tzcheck import refdata
    monkeypatch.setattr(refdata, "gost_status", lambda d: {"designation": d, "title": "Мебель", "status": "отменён", "replaced_by": "ГОСТ 2-2020"})
    look = {"findings": [], "lookups": [{"type": "standard", "value": "ГОСТ 1-1990", "block_id": "t1r2"}]}

    def ref(prompt):
        assert "статус «отменён»" in prompt and "заменён на ГОСТ 2-2020" in prompt
        return {"findings": []}
    s, doc = _prep(stub, {"найти дефекты": lambda p: look if "Стол" in p else {"findings": []}, "проверить обозначения": ref})
    core.find(doc, Scrubber(), CATS)
    core.refs(doc, Scrubber(), CATS)
    assert len(s.asked("проверить обозначения")) == 1


def test_pipeline_without_model_reports_honestly(tmp_path, monkeypatch):
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)
    from docx import Document
    d = Document()
    d.add_paragraph("Поставка стульев. Срок поставки 5 дней.")
    p = tmp_path / "a.docx"
    d.save(p)
    r = pipeline.check(str(p), use_llm=False)
    assert r["findings"] == [] and r["mode"] == "format" and r["steps"] == ["parse", "security"]
    assert r["summary"].startswith("Модель не настроена")


@pytest.mark.parametrize("orch", ["linear", "graph"])
def test_pipeline_with_stub_model(tmp_path, stub, orch):
    from docx import Document
    d = Document()
    d.add_paragraph("Срок поставки: в разумный срок.")
    p = tmp_path / "a.docx"
    d.save(p)
    s = stub({"паспорт": {"passport": PASS["passport"], "roles": []},
              "найти дефекты": {"findings": [{"block_id": "p1", "quote": "в разумный срок", "category": "Некорректная характеристика",
                                              "severity": "high", "explanation": "Срок не определён.", "fix": "Указать число дней.", "confidence": 90}]},
              "независимая вторая проверка": {"decisions": [{"n": 0, "decision": "keep", "reason": "срок не задан", "confidence": 95}]}})
    r = pipeline.check(str(p), use_llm=True, orchestrator=orch)
    steps = ["parse", "security", "passport", "tables", "core_find", "core_cross", "core_verify"]
    assert r["mode"] == "model" and r["steps"] == steps + (["finish"] if orch == "graph" else [])
    assert [(f["quote"], f["section"]) for f in r["findings"]] == [("в разумный срок", "main")]
    assert r["findings"][0]["confidence_pct"] == 95
    assert r["document"]["procurement"]["закон"] == "223-ФЗ"


def test_trace_journal_and_finding_chain(tmp_path, scripted):
    """У каждого замечания цепочка: found с вызовом из журнала, located, verify, final; снятые лежат в журнале отдельно."""
    from tzcheck import jobs
    lift = "samples/lift_src.docx"
    r = pipeline.check(lift, use_llm=True)
    calls = {c["id"]: c for c in r["_trace"]["calls"]}
    assert r["trace_calls"] == len(calls) > 0 and r["trace_rejected"] == len(r["_trace"]["rejected"])
    assert {c["stage"] for c in calls.values()} >= {"passport", "find", "verify"}
    for f in r["findings"]:
        steps = [s["step"] for s in f["trace"]]
        assert steps[0] == "found" and f["trace"][0]["call"] in calls and steps[1] == "located" and steps[-1] == "final"
        assert "verify" in steps
    jobs.write_trace(r, str(tmp_path))
    data = json.load(open(tmp_path / "trace.json", encoding="utf-8"))
    assert all(set(c) >= {"id", "stage", "title", "unit", "model", "temperature", "seconds", "tokens", "prompt", "response", "parsed", "retry", "error"}
               for c in data["calls"])
    assert "_trace" not in json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, ensure_ascii=False, default=str)


def test_rejected_by_verify_in_journal(stub):
    s, doc = _prep(stub, {"найти дефекты": lambda p: {"findings": [_finding()] if "Стол" in p else []},
                          "независимая вторая проверка": {"decisions": [{"n": 0, "decision": "reject", "reason": "значение есть"}]}})
    out = core.verify(doc, Scrubber(), core.find(doc, Scrubber(), CATS))
    assert out == [] and doc.llm_rejected[0].trace[-1]["step"] == "verify"
    assert doc.llm_rejected[0].trace[-1]["text"] == "снято: значение есть" and doc.llm_rejected[0].trace[-1]["call"].startswith("c")


def test_not_located_goes_to_dropped(stub):
    s, doc = _prep(stub, {"найти дефекты": {"findings": [_finding(quote="такого текста нет")]}})
    core.find(doc, Scrubber(), CATS)
    assert doc.journal.dropped and doc.journal.dropped[0]["trace"][-1]["step"] == "not_located"


def test_labelled_row_quote_is_located(stub):
    s, doc = _prep(stub, {"найти дефекты": {"findings": [_finding(block_id="t1r2", quote="Характеристика: Длина | Значение: 1200")]}})
    out = core.find(doc, Scrubber(), CATS)
    assert len(out) == 1 and out[0].block_id == "t1r2" and out[0].quote == "Длина | 1200"


def test_trace_file_only_via_api(tmp_path, monkeypatch, scripted):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from tzcheck import api, feedback
    monkeypatch.setattr(feedback, "PATH", str(tmp_path / "fb.jsonl"))
    monkeypatch.setattr(api, "STORE", str(tmp_path / "runs"))
    import os
    os.makedirs(api.STORE, exist_ok=True)
    c = TestClient(api.app)
    r = c.post("/api/check?use_llm=true", files={"file": ("lift_src.docx", open("samples/lift_src.docx", "rb"), "application/octet-stream")})
    rid = r.json()["run_id"]
    t = c.get(f"/api/runs/{rid}/trace").json()
    assert t["calls"] and t["calls"][0]["prompt"].startswith("Задача: ")
    assert c.get(f"/runs/{rid}/trace.json").status_code == 404
    run = c.get(f"/api/runs/{rid}").json()
    assert run["trace_calls"] == len(t["calls"]) and "calls" not in run and run["findings"][0]["trace"][0]["step"] == "found"
    assert c.get("/api/runs/nope/trace").status_code == 404


def test_pipeline_routes_by_second_check(tmp_path, stub):
    """Подтверждённое второй проверкой идёт в основной список, пониженное ею ядро не снимает, а кладёт в «На проверку»."""
    from docx import Document
    d = Document()
    d.add_paragraph("Срок поставки: в разумный срок.")
    d.add_paragraph("Товар должен соответствовать ГОСТ 12345-67.")
    p = tmp_path / "a.docx"
    d.save(p)
    stub({"паспорт": {"passport": PASS["passport"], "roles": []},
          "найти дефекты": {"findings": [{"block_id": "p1", "quote": "в разумный срок", "category": "Противоречие", "needs_review": True,
                                          "severity": "high", "explanation": "Срок не определён.", "fix": "Указать число дней.", "confidence": 90},
                                         {"block_id": "p2", "quote": "ГОСТ 12345-67", "category": "Стандарты (ГОСТ, поверка)",
                                          "severity": "medium", "explanation": "Стандарт заменён.", "fix": "Указать действующий.", "confidence": 60}]},
          "независимая вторая проверка": {"decisions": [{"n": 0, "decision": "keep", "reason": "срок не задан", "confidence": 88},
                                                        {"n": 1, "decision": "lower", "reason": "статус стандарта вне комплекта", "confidence": 50}]}})
    r = pipeline.check(str(p), use_llm=True)
    assert [(f["quote"], f["section"], f["status"]) for f in r["findings"]] == [("в разумный срок", "main", "ошибка"),
                                                                              ("ГОСТ 12345-67", "llm_review", "ручная проверка")]
    assert r["stats"]["from_model"] == 1 and r["stats"]["by_status"] == {"ошибка": 1}
    assert r["findings"][1]["evidence"]["блок отчёта"] == "на проверку: вторая проверка: понижено: на ручную проверку"
    assert "«На проверку»" in r["summary"]
