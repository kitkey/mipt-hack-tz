import re

import pytest

from tzcheck import feedback, llm
from tzcheck.model import Finding
from tzcheck.pii import Scrubber
from tzcheck.pipeline import check
from tzcheck.report import to_html

FIN = "samples/lift_fin.docx"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "PATH", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
    for k in ("TZCHECK_ORCHESTRATOR", "TZCHECK_RAG", "TZCHECK_LLM_DOC_TIMEOUT", "TZCHECK_LLM_WORKERS"):
        monkeypatch.delenv(k, raising=False)


class Stub:
    """Модель отклоняет все замечания правил и находит одно своё высокого риска."""

    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, max_tokens=4000):
        self.calls += 1
        if prompt.startswith("Кандидаты в ошибки"):
            ns = [int(n) for n in re.findall(r"^(\d+)\. \[", prompt, re.M)]
            return {"decisions": [{"n": n, "decision": "confirm" if "Модель нашла" in prompt else "reject", "reason": "заглушка", "confidence": 80} for n in ns]}
        if "Найди проблемы, которые мешают закупке" in prompt:
            blocks = [b for b in re.findall(r"^\[(p\d+)\] \([^)]*\) (.+)$", prompt, re.M) if len(b[1].split()) >= 8]
            if not blocks or self.calls > 3:
                return {"findings": []}
            bid, text = blocks[0]
            return {"findings": [{"block_id": bid, "quote": " ".join(text.split()[:6]), "category": "Некорректная характеристика", "severity": "high",
                                  "explanation": "Модель нашла неоднозначность.", "fix": "Уточнить.", "norm_ids": [], "needs_review": False, "confidence": 70}]}
        return {"findings": [], "pairs": []}


def test_verify_rules_never_drops_rule_findings(monkeypatch):
    f = Finding("p1", "q", "Лишнее в ТЗ", "x", "y", source="rule:notes", needs_review=True, confidence=0.3)

    class Doc:
        warnings = []

        def block(self, bid):
            return None

    monkeypatch.setattr(llm, "_call", lambda p, m=0: {"decisions": [{"n": 0, "decision": "reject", "reason": "нет", "confidence": 10}]})
    kept = llm.verify_rules(Doc(), [f], Scrubber())
    assert kept == [f] and f.needs_review and "решает закупщик" in f.evidence["вторая проверка"]


def test_model_findings_go_to_separate_block(monkeypatch):
    rules_only = check(FIN, use_llm=False)
    stub = Stub()
    monkeypatch.setattr(llm, "_call", stub)
    r = check(FIN, use_llm=True)
    main = [f for f in r["findings"] if f["section"] == "main"]
    extra = [f for f in r["findings"] if f["section"] == "llm_review"]
    assert len(main) == len(rules_only["findings"])
    assert extra and all(f["id"].startswith("M") and f["needs_review"] and f["status"] == "ручная проверка" for f in extra)
    assert r["stats"]["from_model"] == len(extra)
    assert r["summary"].split(".")[0] == rules_only["summary"].split(".")[0]
    assert "модели на ручную проверку" in r["summary"]
    html = to_html(r)
    assert "Замечания модели: проверить вручную" in html and html.index('id="mrows"') > html.index('id="frows"')


def test_doc_budget_skips_model_calls(monkeypatch):
    stub = Stub()
    monkeypatch.setattr(llm, "_call", stub)
    monkeypatch.setenv("TZCHECK_LLM_DOC_TIMEOUT", "-1")
    r = check(FIN, use_llm=True)
    assert stub.calls == 0
    assert any("бюджет" in w for w in r["warnings"])
    assert all(f["section"] == "main" for f in r["findings"])


class StubTwoStage(Stub):
    """Правила модель отклоняет всегда; свою находку подтверждает с оценкой 30, а при повторной проверке отклоняет."""

    def __init__(self):
        super().__init__()
        self.seen = set()

    def __call__(self, prompt, max_tokens=4000):
        if prompt.startswith("Кандидаты в ошибки"):
            self.calls += 1
            out = []
            for n, item in re.findall(r"^(\d+)\. (\[.*)$", prompt, re.M):
                mine = "Модель нашла" in item
                again = item in self.seen
                self.seen.add(item)
                dec = "reject" if (not mine or again) else "confirm"
                out.append({"n": int(n), "decision": dec, "reason": "заглушка", "confidence": 30 if mine and not again else 20})
            return {"decisions": out}
        return super().__call__(prompt, max_tokens)


@pytest.mark.parametrize("orchestrator", ["linear", "graph"])
def test_block_and_confidence_in_both_orchestrators(monkeypatch, orchestrator):
    rules_only = check(FIN, use_llm=False)
    monkeypatch.setattr(llm, "_call", StubTwoStage())
    r = check(FIN, use_llm=True, orchestrator=orchestrator)
    main = [f for f in r["findings"] if f["section"] == "main"]
    extra = [f for f in r["findings"] if f["section"] == "llm_review"]
    assert r["orchestrator"] == orchestrator
    assert len(main) == len(rules_only["findings"])
    reviewed = [f for f in main if "вторая проверка" in f["evidence"]]
    assert reviewed and all("решает закупщик" in f["evidence"]["вторая проверка"] and f["confidence_pct"] is not None for f in reviewed)
    if orchestrator == "graph":
        assert "llm_recheck" in r["steps"] and not extra
    else:
        assert extra and all(f["confidence_pct"] == 30 and f["confidence_basis"] for f in extra)


class StubInventor(Stub):
    """Пишет заключение с выдумкой, которой нет ни в одном замечании."""

    def __call__(self, prompt, max_tokens=4000):
        if prompt.startswith("По результатам проверки ТЗ"):
            self.calls += 1
            return {"summary": "BW-1 это марка лифта, сверить с бюджетом закупки."}
        return super().__call__(prompt, max_tokens)


@pytest.mark.parametrize("orchestrator", ["linear", "graph"])
def test_summary_built_by_code_and_chips_count_main_only(monkeypatch, orchestrator):
    rules_only = check(FIN, use_llm=False, orchestrator=orchestrator)
    monkeypatch.setattr(llm, "_call", StubInventor())
    r = check(FIN, use_llm=True, orchestrator=orchestrator)
    main = [f for f in r["findings"] if f["section"] == "main"]
    extra = [f for f in r["findings"] if f["section"] == "llm_review"]
    tail = " Отдельным блоком" if extra else ""
    assert r["summary"].startswith(rules_only["summary"]) and r["summary"][len(rules_only["summary"]):].startswith(tail)
    assert "марка лифта" not in r["summary"] and "бюджет" not in r["summary"]
    st = r["stats"]
    assert sum(st["by_severity"].values()) == sum(st["by_status"].values()) == sum(st["by_category"].values()) == len(main)
    assert st["model_block"]["count"] == len(extra) and sum(st["model_block"]["by_severity"].values()) == len(extra)
    assert "марка лифта" in st["model_block"]["note"]
    html = to_html(r)
    head = html[:html.index('id="frows"')]
    assert "марка лифта" not in head and f'id="cshown">{len(main)}<' in head
    assert html.index("марка лифта") > html.index("Замечания модели: проверить вручную") > html.index('id="frows"')
    assert "не входит" in html[html.index("марка лифта"):]
