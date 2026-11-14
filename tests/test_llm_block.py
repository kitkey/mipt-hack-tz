"""Модель в конвейере: бюджет времени на документ, практика заказчика только по паспорту, сбой вызова не ломает отчёт."""
import pytest

from tzcheck import feedback, llm
from tzcheck.pipeline import check
from tzcheck.report import to_html

FIN = "samples/lift_fin.docx"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "PATH", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    for k in ("TZCHECK_ORCHESTRATOR", "TZCHECK_RAG", "TZCHECK_LLM_DOC_TIMEOUT", "TZCHECK_CUSTOMER"):
        monkeypatch.delenv(k, raising=False)


def test_doc_budget_skips_model_calls(scripted, monkeypatch):
    monkeypatch.setenv("TZCHECK_LLM_DOC_TIMEOUT", "-1")
    r = check(FIN, use_llm=True)
    assert scripted.prompts == []
    assert any("бюджет" in w for w in r["warnings"]) and r["findings"] == []


def test_failed_call_is_reported_not_raised(scripted, monkeypatch):
    def broken(prompt, max_tokens=4000):
        raise RuntimeError("сеть недоступна")
    monkeypatch.setattr(llm, "_call", broken)
    r = check(FIN, use_llm=True)
    # сбой модели не роняет проверку, но и не выдаётся за чистый результат: шаги с упавшими вызовами помечены ошибкой
    assert r["status"] == "incomplete" and r["findings"] == [] and r["verdict_code"] == "incomplete"
    assert any("шаг «паспорт и роли» пропущен" in w for w in r["warnings"])
    assert "passport" in r["completeness"]["incomplete_steps"] and r["summary"].startswith("Проверка неполная")


@pytest.mark.parametrize("profile,shown", [("", False), ("mipt", True)])
def test_practice_only_for_customer_from_passport(scripted, monkeypatch, profile, shown):
    orig = scripted.__call__

    class Wrap:
        prompts = scripted.prompts

        def __call__(self, prompt, max_tokens=4000):
            if prompt.startswith("Задача: паспорт"):
                scripted.prompts.append(prompt)
                return {"passport": {"law": "223-ФЗ", "kind": "товар", "subject": "лифт", "customer": "x", "customer_profile": profile}, "roles": []}
            return orig(prompt, max_tokens)
    monkeypatch.setattr(llm, "_call", Wrap())
    r = check(FIN, use_llm=True)
    find = [p for p in scripted.prompts if p.startswith("Задача: найти дефекты")]
    assert find and all(("Практика закупщиков заказчика (МФТИ)" in p) == shown for p in find)
    assert r["document"]["procurement"]["профиль заказчика"] == profile


def test_findings_of_core_routed_by_second_check_and_in_report(scripted):
    """Основной список: подтверждённое второй проверкой; пониженное ею идёт в раздел «На проверку»."""
    scripted.lower_every = 2
    r = check(FIN, use_llm=True)
    main = [f for f in r["findings"] if f["section"] == "main"]
    review = [f for f in r["findings"] if f["section"] == "llm_review"]
    assert main and review and len(main) + len(review) == len(r["findings"])
    assert all(f["evidence"]["вторая проверка"] == "подтверждено" and not f["needs_review"] for f in main)
    assert all(f["evidence"]["блок отчёта"].startswith("на проверку: вторая проверка: понижено") for f in review)
    assert r["stats"]["from_model"] == len(review) and r["verdict"]
    html = to_html(r)
    assert main[0]["quote"] in html and f"На проверку ({len(review)})" in html
