"""Ответ модели неверной схемы (число, строка, null вместо списка или словаря, элементы списка не словари) значит, что шаг
модель не ответила: проверка не падает и доходит до отчёта."""
import json
import sys

import docx
import pytest
from conftest import fake_litellm

from tzcheck import fixcheck, pipeline
from tzcheck.rag import verify

FINDING = {"block_id": "p1", "quote": "в разумный срок", "category": "Некорректная характеристика", "severity": ["high"],
           "explanation": "Срок не определён.", "fix": "Указать число дней.", "replacement": "в течение 10 рабочих дней",
           "norm_ids": 17, "confidence": 90}
KEYS = ("findings", "decisions", "items", "lookups", "roles", "passport", "columns")


def _bad(value):
    return {k: value for k in KEYS}


@pytest.fixture
def check(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
    monkeypatch.setenv("TZCHECK_LLM_WORKERS", "1")
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    for name in ("TZCHECK_RAG", "TZCHECK_FIXCHECK", "TZCHECK_FIXCHECK_MODEL", "TZCHECK_ORCHESTRATOR"):
        monkeypatch.delenv(name, raising=False)
    d = docx.Document()
    d.add_paragraph("Срок поставки: в разумный срок.")
    d.add_paragraph("Поставка в рабочие дни, приёмка по акту.")
    path = tmp_path / "tz.docx"
    d.save(path)

    def go(answer, orch="linear"):
        def model(prompt):
            if "Верни JSON: {\"summary\"" in prompt:
                return {"summary": "Заключение заглушки."}
            return json.loads(json.dumps(answer(prompt)))
        monkeypatch.setitem(sys.modules, "litellm", fake_litellm(model))
        return pipeline.check(str(path), use_llm=True, orchestrator=orch)
    return go


@pytest.mark.parametrize("orch", ["linear", "graph"])
@pytest.mark.parametrize("value", [17, "x", None, {"n": 1}, ["x"], [17, None, ["x"]]])
def test_wrong_types_everywhere_do_not_crash(check, orch, value):
    r = check(lambda p: _bad(value), orch)
    assert r["status"] in ("ok", "incomplete") and r["findings"] == []


@pytest.mark.parametrize("orch", ["linear", "graph"])
@pytest.mark.parametrize("decisions", [{"decisions": 17}, {"decisions": ["x"]}, {"decisions": None}, {"decisions": "keep"}])
def test_wrong_decisions_mean_no_answer(check, orch, decisions):
    def answer(p):
        if p.startswith("Задача: найти дефекты"):
            return {"findings": [FINDING, "x", 17], "lookups": 17}
        return decisions
    r = check(answer, orch)
    assert r["status"] in ("ok", "incomplete") and len(r["findings"]) == 1
    f = r["findings"][0]
    assert f["norm_ids"] == [] and f["severity"] == "medium" and r["stats"]["revisions"] == 0
    assert f["section"] != "main" or f["evidence"]["проверка правки"].startswith("правка не проверена")


@pytest.mark.parametrize("data", [{"decisions": 17}, {"decisions": ["x"]}, {"decisions": {"n": 1}}, None, 17])
def test_verify_decisions_wrong_types(data):
    assert verify._decisions(data) == []

