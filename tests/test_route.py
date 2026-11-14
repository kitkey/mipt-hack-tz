"""Раздел отчёта для замечания ядра по решению второй проверки: основной список или «на проверку», ничего не снимается."""
import pytest

from tzcheck import pipeline
from tzcheck.model import SECTION_LLM, SECTION_MAIN, SECTION_UNCHECKED, Finding


def make(verdict="подтверждено", pct=95, source="model:find", absent=False, section=SECTION_MAIN, status="ошибка", review=False):
    f = Finding("p1", "в разумный срок", "Противоречие", "Срок не определён.", "Указать срок.", "high", [], review, source, 0.7, "абзац 1")
    f.status, f.confidence_pct, f.section = status, pct, section
    if verdict:
        f.evidence["вторая проверка"] = verdict
    if absent:
        f._absent = True
    return f


def test_confirmed_by_second_check_stays_main():
    f = pipeline.route(make())
    assert f.section == SECTION_MAIN and not f.needs_review
    assert f.trace[-1]["step"] == "route" and f.trace[-1]["text"].startswith("основной список")


def test_confirmation_clears_review_mark_of_find_step():
    f = pipeline.route(make(status="ручная проверка", review=True, pct=70))
    assert f.section == SECTION_MAIN and not f.needs_review and f.status == "ошибка"


@pytest.mark.parametrize("kw, why", [
    ({"verdict": "понижено: на ручную проверку"}, "вторая проверка: понижено"),
    ({"verdict": "не проведена"}, "вторая проверка: не проведена"),
    ({"verdict": ""}, "вторая проверка: не проведена"),
    ({"absent": True}, "«не указано»"),
])
def test_other_findings_go_to_review_with_reason(kw, why):
    f = pipeline.route(make(**kw))
    assert f.section == SECTION_LLM and f.needs_review
    assert f.evidence["блок отчёта"].startswith("на проверку: ") and why in f.evidence["блок отчёта"]
    assert f.trace[-1]["step"] == "route" and why in f.trace[-1]["text"]


def test_absent_by_evidence_goes_to_review():
    f = make()
    f.evidence["проверка «не указано»"] = "не найдено по комплекту"
    assert pipeline.route(f).section == SECTION_LLM


def test_unapproved_gost_item_goes_to_review():
    f = make(source="model:gost", review=True, status="ручная проверка")
    f.evidence["пункт ГОСТ"] = {"designation": "ГОСТ 33984.1-2023", "clause": "5.2", "status": "verified"}
    f = pipeline.route(f)
    assert f.section == SECTION_LLM and "не утверждён человеком" in f.evidence["блок отчёта"]


def test_optional_threshold_from_env(monkeypatch):
    assert pipeline.route(make(pct=40)).section == SECTION_MAIN
    monkeypatch.setenv("TZCHECK_MAIN_MIN_CONFIDENCE", "80")
    assert pipeline.route(make(pct=85)).section == SECTION_MAIN
    f = pipeline.route(make(pct=75))
    assert f.section == SECTION_LLM and "уверенность второй проверки 75 %" in f.evidence["блок отчёта"]
    assert pipeline.route(make(pct=None)).section == SECTION_LLM
    monkeypatch.setenv("TZCHECK_MAIN_MIN_CONFIDENCE", "x")
    assert pipeline.route(make(pct=10)).section == SECTION_MAIN


def test_non_core_and_other_sections_untouched():
    rule = pipeline.route(make(source="rule:units", status="неясно", pct=None, verdict=""))
    assert rule.section == SECTION_MAIN and not rule.trace
    unchecked = pipeline.route(make(section=SECTION_UNCHECKED, status="ручная проверка"))
    assert unchecked.section == SECTION_UNCHECKED
