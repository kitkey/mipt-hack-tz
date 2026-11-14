import json
import os
import re

import docx
import pytest

from tzcheck import confidence, feedback, llm
from tzcheck.model import Finding
from tzcheck.pipeline import check
from tzcheck.report import to_docx_comments, to_html, write_all

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
LIFT = os.path.join(SAMPLES, "lift_src.docx")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "PATH", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    monkeypatch.delenv("TZCHECK_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("TZCHECK_CONF_LOW", raising=False)
    monkeypatch.delenv("TZCHECK_RAG", raising=False)


class StubModel:
    """Заглушка модели: отвечает JSON по типу промпта, первой находке даёт низкую уверенность, второй мусор вместо числа."""

    def __init__(self, verify_score=90, recheck_score=85, find_scores=(35, "много")):
        self.verify_score, self.recheck_score, self.find_scores = verify_score, recheck_score, find_scores
        self.seen = set()

    def __call__(self, prompt, max_tokens=4000):
        if prompt.startswith("Кандидаты в ошибки"):
            out = []
            for n, item in re.findall(r"^(\d+)\. (\[.*)$", prompt, re.M):
                again = item in self.seen
                self.seen.add(item)
                out.append({"n": int(n), "decision": "confirm", "reason": "по тексту блока",
                            "confidence": self.recheck_score if again else self.verify_score})
            return {"decisions": out}
        if "Найди проблемы, которые мешают закупке" in prompt:
            blocks = re.findall(r"^\[(p\d+)\] \([^)]*\) (.+)$", prompt, re.M)
            out = []
            for (bid, text), score in zip([b for b in blocks if len(b[1].split()) >= 8][:2], self.find_scores):
                out.append({"block_id": bid, "quote": " ".join(text.split()[:6]), "category": "Некорректная характеристика",
                            "severity": "medium", "explanation": "Формулировка допускает разное толкование.",
                            "fix": "Уточнить формулировку.", "norm_ids": [], "needs_review": False, "confidence": score})
            return {"findings": out}
        if "Верни JSON: {\"summary\"" in prompt:
            return {"summary": "Заключение заглушки."}
        return {"findings": [], "pairs": []}


@pytest.fixture
def stub(monkeypatch):
    s = StubModel()
    monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
    monkeypatch.setenv("TZCHECK_LLM_WORKERS", "1")
    monkeypatch.setattr(llm, "_call", s)
    return s


@pytest.mark.parametrize("value,expected", [(85, 85), ("70", 70), ("90 %", 90), (0.8, 80), (0, 0), (100, 100), (99.6, 100),
                                            (101, None), (-5, None), ("много", None), (None, None), (True, None), ([], None), (float("nan"), None)])
def test_parse_pct(value, expected):
    assert confidence.parse_pct(value) == expected


def test_missing_score_is_marked_not_invented():
    f = Finding("p1", "q", "Лишнее в ТЗ", "x", "y", source="llm")
    confidence.set_model_score(f, None, confidence.STAGE_FIND)
    confidence.apply([f])
    assert f.confidence_pct is None and confidence.NO_SCORE in f.confidence_basis
    g = confidence.set_model_score(Finding("p1", "q", "Лишнее в ТЗ", "x", "y", source="llm"), 72, confidence.STAGE_FIND)
    confidence.set_model_score(g, "abc", confidence.STAGE_VERIFY)
    assert g.confidence_pct == 72 and g.confidence_basis == confidence.STAGE_FIND
    confidence.set_model_score(g, 40, confidence.STAGE_VERIFY)
    assert g.confidence_pct == 40 and g.evidence["уверенность (модель при поиске)"] == "72 %"


def test_rules_without_model_have_no_number():
    r = check(LIFT, use_llm=False)
    assert r["findings"]
    for f in r["findings"]:
        assert f["confidence_pct"] is None
        assert f["confidence_basis"] == confidence.RULE
    assert r["stats"]["confidence"]["правило"] == len(r["findings"])


def test_prompts_ask_for_confidence():
    for p in (llm.CHUNK_PROMPT, llm.VERIFY_PROMPT, llm.CROSS_PROMPT):
        assert '"confidence": целое 0-100' in p
        p.format(blocks="", known="", title="", fields="", categories="", norms="", items="", facts="", subject="")


def test_model_scores_flow_to_findings(stub):
    r = check(LIFT, use_llm=True)
    llm_f = [f for f in r["findings"] if f["source"].startswith("llm")]
    assert llm_f, "заглушка должна дать находки модели"
    assert all(f["confidence_pct"] == 90 and f["confidence_basis"] == confidence.STAGE_VERIFY for f in llm_f)
    assert any(f["evidence"].get("уверенность (модель при поиске)") == "35 %" for f in llm_f)
    verified = [f for f in r["findings"] if f["source"].startswith("rule") and "llm" in f["source"]]
    assert all(f["confidence_pct"] == 90 for f in verified)
    plain = [f for f in r["findings"] if f["source"].startswith("rule") and "llm" not in f["source"]]
    assert all(f["confidence_pct"] is None and f["confidence_basis"] == confidence.RULE for f in plain)
    assert r["orchestrator"] == "linear" and "llm_recheck" not in r["steps"]


def test_verify_without_score_keeps_honest_mark(stub):
    stub.verify_score = None
    r = check(LIFT, use_llm=True)
    llm_f = [f for f in r["findings"] if f["source"].startswith("llm")]
    first = [f for f in llm_f if f["confidence_pct"] == 35]
    assert first and first[0]["confidence_basis"] == confidence.STAGE_FIND
    unscored = [f for f in llm_f if f["confidence_pct"] is None]
    assert unscored and all(confidence.NO_SCORE in f["confidence_basis"] for f in unscored)


def _key(f):
    return f["category"], f["block_id"], f["quote"]


@pytest.mark.parametrize("name", ["lift_src.docx", "metro_fin.docx"])
def test_graph_equals_linear_without_model(name):
    a = check(os.path.join(SAMPLES, name), use_llm=False)
    b = check(os.path.join(SAMPLES, name), use_llm=False, orchestrator="graph")
    assert b["orchestrator"] == "graph" and b["steps"] == ["parse", "security", "rules", "reconcile", "finish"]
    assert a["findings"] == b["findings"]
    assert set(a) == set(b)


def test_graph_rechecks_low_confidence(stub):
    stub.verify_score = 40
    lin = check(LIFT, use_llm=True)
    assert all(f["confidence_pct"] == 40 for f in lin["findings"] if f["source"].startswith("llm"))
    stub.seen.clear()
    g = check(LIFT, use_llm=True, orchestrator="graph")
    assert "llm_recheck" in g["steps"]
    llm_f = [f for f in g["findings"] if f["source"].startswith("llm")]
    assert llm_f and all(f["confidence_pct"] == 85 and f["confidence_basis"] == confidence.STAGE_RECHECK for f in llm_f)
    assert all(f["evidence"]["уверенность (модель при второй проверке)"] == "40 %" for f in llm_f)
    assert {k for k in lin} == {k for k in g}


def test_graph_skips_recheck_when_confident(stub):
    g = check(LIFT, use_llm=True, orchestrator="graph")
    assert "llm_recheck" not in g["steps"] and "llm_find" in g["steps"]


def test_recheck_rejects_model_finding():
    doc_f = Finding("p1", "q", "Лишнее в ТЗ", "x", "y", source="llm", confidence_pct=30, confidence_basis=confidence.STAGE_VERIFY)
    rule_f = Finding("p2", "r", "Лишнее в ТЗ", "x", "y", source="rule:notes+llm", confidence_pct=20, confidence_basis=confidence.STAGE_VERIFY)

    class Doc:
        warnings = []

        def block(self, bid):
            return None

    orig = llm._call
    llm._call = lambda p, m=0: {"decisions": [{"n": 0, "decision": "reject", "reason": "нет", "confidence": 10},
                                              {"n": 1, "decision": "reject", "reason": "нет", "confidence": 15}]}
    try:
        kept = llm.recheck(Doc(), [doc_f, rule_f], __import__("tzcheck.pii", fromlist=["Scrubber"]).Scrubber())
    finally:
        llm._call = orig
    assert kept == [rule_f] and rule_f.needs_review and rule_f.confidence_pct == 15


def test_unknown_orchestrator():
    with pytest.raises(ValueError):
        check(LIFT, use_llm=False, orchestrator="tree")


def test_reports_show_confidence(stub, tmp_path):
    r = check(LIFT, use_llm=True)
    page = to_html(r)
    assert "<th>Уверенность</th>" in page and 'id="cmin"' in page and 'id="csort"' in page
    assert 'data-conf="90"' in page and 'data-conf=""' in page
    assert "90 %" in page and confidence.RULE in page
    paths = write_all(r, LIFT, str(tmp_path))
    data = json.load(open(paths["json"], encoding="utf-8"))
    assert all("confidence_pct" in f and "confidence_basis" in f for f in data["findings"])
    texts = []
    d = docx.Document(paths["docx"])
    for c in d.comments:
        texts.append(c.text)
    assert any("уверенность: 90 % (модель при второй проверке)" in t for t in texts)
    assert any("уверенность: правило" in t for t in texts)


def test_html_threshold_default_from_env(monkeypatch):
    monkeypatch.setenv("TZCHECK_CONF_MIN", "70")
    r = check(LIFT, use_llm=False)
    assert 'value="70"' in to_html(r)
