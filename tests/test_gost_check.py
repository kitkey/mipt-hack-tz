"""Сверка позиций с чеклистами ГОСТ: обозначения назвала модель на поиске, применимость и сверку решает модель,
вызовы о применимости в журнале с этапом gost."""
import sys

import pytest
from conftest import fake_litellm
from test_gost_profiles import FakeModel, fake_embed, standard

from tzcheck import core, gost_check, llm, passport, tables
from tzcheck import gost_profiles as gp
from tzcheck.parse import Block, ParsedDoc
from tzcheck.pii import Scrubber

CATS = list(core.CATEGORIES)


def _row(bid, row, cells):
    return Block(bid, "row", " | ".join(cells), "Спецификация", 0, row, list(cells), grid=list(cells), hmerge=[False] * len(cells),
                 vmerge=[False] * len(cells))


def lift_doc(name="Лифт грузовой"):
    blocks = [Block("p1", "paragraph", "Лифт должен соответствовать ГОСТ 99999-2020.", "Общие требования"),
              _row("t1r1", 0, ["№", "Наименование", "Характеристика", "Значение"]),
              _row("t1r2", 1, ["1", name, "Номинальная скорость", "2,5 м/с"])]
    return ParsedDoc("tz.docx", blocks, {}, [], "docx")


HEADER = {"header_rows": 1, "kind": "positions", "item": "",
          "columns": [{"index": 0, "role": "num", "label": "№"}, {"index": 1, "role": "name", "label": "Наименование"},
                      {"index": 2, "role": "char_name", "label": "Характеристика"}, {"index": 3, "role": "value", "label": "Значение"}]}


class Model:
    def __init__(self, item_answer, lookups=True):
        self.item_answer, self.lookups, self.prompts = item_answer, lookups, []
        self.gp = FakeModel(applies=("Лифт", "лифт"))

    def __call__(self, prompt, max_tokens=4000):
        self.prompts.append(prompt)
        if prompt.startswith(gp.SYSTEM):
            return self.gp(prompt[len(gp.SYSTEM):].lstrip())
        if prompt.startswith("Задача: разметка шапки"):
            return HEADER
        if prompt.startswith("Задача: найти дефекты"):
            lk = [{"type": "standard", "value": "ГОСТ 99999-2020", "block_id": "p1"}] if self.lookups else []
            return {"findings": [], "lookups": lk}
        if prompt.startswith("Задача: сверить позицию"):
            return self.item_answer(prompt)
        return {}

    def asked(self, key):
        return [p for p in self.prompts if p.removeprefix(gp.SYSTEM).lstrip().startswith(key)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_GOST_PROFILES", str(tmp_path))
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)
    monkeypatch.setattr(gp, "_texts", lambda: {"ГОСТ 99999-2020": (standard(), "x")})
    monkeypatch.setattr(gp, "_embed", fake_embed)
    monkeypatch.setattr(gp, "PASSES", 1)
    gp.BUILD_ERRORS.clear()
    gp.build("ГОСТ 99999-2020", complete=FakeModel())

    def run(model, doc):
        monkeypatch.setitem(sys.modules, "litellm", fake_litellm(model))
        monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
        doc.journal, doc.model_dropped = llm.Journal(), []
        doc.passport = dict(passport.EMPTY)
        tables.build(doc, Scrubber())
        core.find(doc, Scrubber(), CATS)
        return gost_check.check(doc, Scrubber(), CATS)
    return run


def speed_contradiction(prompt):
    n = next(int(line.split(".")[0]) for line in prompt.splitlines() if "; скорость;" in line)
    return {"findings": [{"item": n, "block_id": "t1r2", "quote": "Номинальная скорость | 2,5 м/с", "category": "Стандарты (ГОСТ, поверка)",
                          "severity": "high", "explanation": "Скорость выше предела пункта 2.1.", "fix": "Не более 1,0 м/с.", "confidence": 90}]}


def test_contradiction_with_checklist_item_is_found_and_traced(env):
    m = Model(speed_contradiction)
    doc = lift_doc()
    out = env(m, doc)
    assert len(out) == 1
    f = out[0]
    assert f.block_id == "t1r2" and f.source == "model:gost"
    assert f.evidence["пункт ГОСТ"]["clause"] == "2.1" and f.evidence["пункт ГОСТ"]["status"] == "verified"
    assert f.needs_review  # пункт сверен моделью, человек его не утвердил
    stages = [c["stage"] for c in doc.journal.calls]
    assert stages.count("gost") == 1 and stages.count("gost_check") == 1
    gost_step = next(s for s in f.trace if s["step"] == "gost")
    assert gost_step["call"] in {c["id"] for c in doc.journal.calls if c["stage"] == "gost"}
    assert gost_step["item"]["clause"] == "2.1"
    assert [s["step"] for s in f.trace][:3] == ["found", "located", "gost"]
    # в запрос сверки ушли пункты чеклиста и строка позиции
    ask = m.asked("Задача: сверить позицию")[0]
    assert "п. 2.1" in ask and "[t1r2]" in ask


def test_approved_item_keeps_model_severity(env):
    prof = gp.load("ГОСТ 99999-2020")
    sid = next(it["id"] for it in prof["items"] if it["param"] == "скорость")
    gp.approve(prof["slug"], [sid], "Петров")
    out = env(Model(speed_contradiction), lift_doc())
    assert out and out[0].evidence["пункт ГОСТ"]["status"] == "approved" and not out[0].needs_review


def test_absent_parameter_goes_to_review(env):
    def absent(prompt):
        n = next(int(line.split(".")[0]) for line in prompt.splitlines() if "; условия окружающей среды;" in line)
        return {"findings": [{"item": n, "block_id": "t1r2", "quote": "Лифт грузовой", "category": "Неполнота или комплектность",
                              "severity": "high", "explanation": "Не указаны условия окружающей среды.", "absent": True,
                              "missing": "условия окружающей среды", "confidence": 70}]}
    out = env(Model(absent), lift_doc())
    assert len(out) == 1 and out[0].needs_review and out[0]._absent


def test_no_standard_named_no_gost_calls(env):
    doc = lift_doc()
    m = Model(speed_contradiction, lookups=False)
    assert env(m, doc) == []
    assert not m.asked("Позиция закупки") and not m.asked("Задача: сверить позицию")
    assert not [c for c in doc.journal.calls if c["stage"] in ("gost", "gost_check")]


def test_not_applicable_position_no_check_call(env):
    doc = lift_doc("Бумага офисная")
    m = Model(speed_contradiction)
    assert env(m, doc) == []
    assert len(m.asked("Позиция закупки")) == 1 and not m.asked("Задача: сверить позицию")
    assert [c["stage"] for c in doc.journal.calls].count("gost") == 1


def test_quote_not_in_text_is_dropped(env):
    def bad(prompt):
        r = speed_contradiction(prompt)
        r["findings"][0]["quote"] = "скорость 4 м/с по паспорту"
        return r
    doc = lift_doc()
    assert env(Model(bad), doc) == []
    assert doc.journal.dropped


def test_items_for_runs_inside_doc_scope(env, monkeypatch):
    """items_for вызывается внутри llm.doc_scope(doc): закрытый документ без NER не уходит во внешнюю модель."""
    from contextlib import contextmanager
    seen = []
    real = gp.items_for

    @contextmanager
    def doc_scope(doc):
        seen.append(("enter", doc.name))
        yield
        seen.append(("exit", doc.name))

    def spy(position, pp, complete=None):
        seen.append(("items_for", position["name"]))
        return real(position, pp, complete)

    monkeypatch.setattr(llm, "doc_scope", doc_scope, raising=False)
    monkeypatch.setattr(gp, "items_for", spy)
    env(Model(speed_contradiction), lift_doc())
    i = seen.index(("items_for", "Лифт грузовой"))
    assert seen[i - 1] == ("enter", "tz.docx") and ("exit", "tz.docx") in seen[i + 1:]
