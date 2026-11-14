"""Раздел отчёта для замечания ядра по его собственным полям: основной список или «на проверку», ничего не снимается."""
import pytest

from tzcheck import pipeline
from tzcheck.model import SECTION_LLM, SECTION_MAIN, SECTION_UNCHECKED, Finding


def make(status="ошибка", pct=95, source="model:find", absent=False, section=SECTION_MAIN):
    f = Finding("p1", "в разумный срок", "Противоречие", "high", "Срок не определён.", "Указать срок.", [], False, source, 0.7, "абзац 1")
    f.status, f.confidence_pct, f.section = status, pct, section
    if absent:
        f._absent = True
    return f


def test_confirmed_error_with_high_confidence_stays_main():
    f = pipeline.route(make())
    assert f.section == SECTION_MAIN and not f.needs_review
    assert f.trace[-1]["step"] == "route" and f.trace[-1]["text"].startswith("основной список")


@pytest.mark.parametrize("kw, why", [
    ({"status": "неясно"}, "статус «неясно»"),
    ({"status": "ручная проверка"}, "статус «ручная проверка»"),
    ({"pct": 89}, "уверенность второй проверки 89 %"),
    ({"pct": None}, "нет оценки"),
    ({"absent": True}, "«не указано»"),
])
def test_other_findings_go_to_review_with_reason(kw, why):
    f = pipeline.route(make(**kw))
    assert f.section == SECTION_LLM and f.needs_review
    assert f.evidence["блок отчёта"].startswith("на проверку: ") and why in f.evidence["блок отчёта"]
    assert f.trace[-1]["step"] == "route" and why in f.trace[-1]["text"]


def test_absent_by_evidence_goes_to_review():
    f = make()
    f.evidence["проверка «не указано»"] = "оставлено: не найдено по комплекту"
    assert pipeline.route(f).section == SECTION_LLM


def test_threshold_from_env(monkeypatch):
    monkeypatch.setenv("TZCHECK_MAIN_MIN_CONFIDENCE", "80")
    assert pipeline.route(make(pct=85)).section == SECTION_MAIN
    monkeypatch.setenv("TZCHECK_MAIN_MIN_CONFIDENCE", "x")
    assert pipeline.route(make(pct=85)).section == SECTION_LLM


def test_non_core_and_other_sections_untouched():
    rule = pipeline.route(make(source="rule:units", status="неясно", pct=None))
    assert rule.section == SECTION_MAIN and not rule.trace
    unchecked = pipeline.route(make(section=SECTION_UNCHECKED, status="ручная проверка"))
    assert unchecked.section == SECTION_UNCHECKED

