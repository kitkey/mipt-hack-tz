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


def test_prompts_ask_for_confidence():
    from tzcheck import core
    for p in (core.FIND_PROMPT, core.REF_PROMPT, core.CROSS_PROMPT, core.VERIFY_PROMPT):
        assert '"confidence":' in p


def test_model_scores_flow_to_findings(scripted):
    r = check(LIFT, use_llm=True)
    assert r["findings"]
    assert all(f["confidence_pct"] == 90 and f["confidence_basis"] == confidence.STAGE_VERIFY for f in r["findings"])
    assert all(f["evidence"].get("уверенность (модель при поиске)") == "60 %" for f in r["findings"])


def test_verify_without_score_keeps_find_score(scripted):
    scripted.verify_score = None
    r = check(LIFT, use_llm=True)
    assert r["findings"] and all(f["confidence_pct"] == 60 and f["confidence_basis"] == confidence.STAGE_FIND for f in r["findings"])
    scripted.find_score = "много"
    r = check(LIFT, use_llm=True)
    assert all(f["confidence_pct"] is None and confidence.NO_SCORE in f["confidence_basis"] for f in r["findings"])


@pytest.mark.parametrize("name", ["lift_src.docx", "metro_fin.docx"])
def test_graph_equals_linear(scripted, name):
    a = check(os.path.join(SAMPLES, name), use_llm=True)
    b = check(os.path.join(SAMPLES, name), use_llm=True, orchestrator="graph")
    assert b["orchestrator"] == "graph" and b["steps"] == a["steps"] + ["finish"]
    key = lambda r: [(f["id"], f["category"], f["block_id"], f["quote"]) for f in r["findings"]]
    assert key(a) == key(b) and set(a) == set(b)


def test_unknown_orchestrator():
    with pytest.raises(ValueError):
        check(LIFT, use_llm=False, orchestrator="tree")


def test_reports_show_confidence(scripted, tmp_path):
    r = check(LIFT, use_llm=True)
    page = to_html(r)
    assert "<th>Уверенность</th>" in page and 'id="cmin"' in page and 'id="csort"' in page
    assert 'data-conf="90"' in page and "90 %" in page
    paths = write_all(r, LIFT, str(tmp_path))
    data = json.load(open(paths["json"], encoding="utf-8"))
    assert all("confidence_pct" in f and "confidence_basis" in f for f in data["findings"])
    texts = []
    d = docx.Document(paths["docx"])
    for c in d.comments:
        texts.append(c.text)
    assert any("уверенность: 90 % (модель при второй проверке)" in t for t in texts)


def test_html_threshold_default_from_env(monkeypatch):
    monkeypatch.setenv("TZCHECK_CONF_MIN", "70")
    r = check(LIFT, use_llm=False)
    assert 'value="70"' in to_html(r)
