import os
import threading

import pytest

from tzcheck import feedback, llm, pii
from tzcheck.model import Finding
from tzcheck.parse import Block
from tzcheck.pipeline import check

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIFT = os.path.join(ROOT, "samples", "lift_src.docx")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "PATH", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    monkeypatch.delenv("TZCHECK_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("TZCHECK_RAG", raising=False)
    monkeypatch.setenv("TZCHECK_PII_NER", "0")


def test_locate_does_not_invent_quote():
    blocks = [Block(id="p1", kind="para", text="Поставщик обязан поставить товар в течение 10 дней с даты заявки.", section="")]
    scr = pii.Scrubber(ner=False)
    assert llm._locate("поставить товар в течение 10 дней", blocks, "p1", scr)[1] == "поставить товар в течение 10 дней"
    assert llm._locate("товар поставить обязан поставщик в течение дней", blocks, "p1", scr) is None


def test_scrubber_threads_one_token_per_value():
    scr = pii.Scrubber(ner=False)
    values = [f"value-{i % 50}" for i in range(4000)]
    toks = {}
    lock = threading.Lock()

    def work(chunk):
        for v in chunk:
            t = scr._token("PERSON", v)
            with lock:
                toks.setdefault(v, set()).add(t)

    th = [threading.Thread(target=work, args=(values[i::8],)) for i in range(8)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert all(len(s) == 1 for s in toks.values())
    assert len(set(scr.mapping.values())) == len(scr.mapping) == 50


def _fd(i="F1", quote="Референс: ТР 5006М", rule="brand.ref"):
    return {"id": i, "category": "Ограничение конкуренции", "source": "rule:brand", "rule_id": rule, "quote": quote}


def _fnd(quote="Референс: ТР 5006М", rule="brand.ref"):
    return Finding("t1r1", quote, "Ограничение конкуренции", "x", "y", source="rule:brand", rule_id=rule, confidence=0.8)


def test_feedback_same_click_twice_counts_once():
    feedback.record("r1", "a.docx", _fd(), "reject")
    feedback.record("r1", "a.docx", _fd(), "reject")
    assert len(feedback.apply([_fnd()])) == 1
    feedback.record("r2", "b.docx", _fd(), "reject")
    assert feedback.apply([_fnd()]) == []


def test_feedback_last_decision_wins_and_review_is_not_a_vote():
    feedback.record("r1", "a.docx", _fd(), "reject")
    feedback.record("r1", "a.docx", _fd(), "accept")
    feedback.record("r2", "b.docx", _fd(), "reject")
    assert len(feedback.apply([_fnd()])) == 1
    for i in range(6):
        feedback.record(f"q{i}", "c.docx", _fd(quote=f"модель {i}"), "review")
    g = feedback.apply([_fnd(quote="новая модель")])
    assert not g[0].needs_review and "обратная связь" not in g[0].evidence
    assert feedback.stats()["brand.ref"]["всего"] == 2


def test_feedback_one_run_votes_once_per_rule():
    for i in range(6):
        feedback.record("r1", "a.docx", _fd(i=f"F{i}", quote=f"модель {i}"), "reject")
    assert not feedback.apply([_fnd(quote="новая модель")])[0].needs_review
    for i in range(5):
        feedback.record(f"r{i + 2}", "a.docx", _fd(quote=f"модель {i}"), "reject")
    g = feedback.apply([_fnd(quote="новая модель")])[0]
    assert g.needs_review and "в 6 из 6 проверок" in g.evidence["обратная связь"]


def test_feedback_scoped_to_customer():
    feedback.record("r1", "a.docx", _fd(), "reject", customer="mipt")
    feedback.record("r2", "b.docx", _fd(), "reject", customer="mipt")
    assert feedback.apply([_fnd()], "mipt") == []
    assert len(feedback.apply([_fnd()], "")) == 1


def test_check_empty_list_is_an_error_not_a_crash():
    r = check([], use_llm=False)
    assert r["status"] == "error"


def test_passport_has_code_version_and_prompt_hash():
    p = check(LIFT, use_llm=False)["passport"]
    assert p["версия кода"] and len(p["промпты sha256"]) == 16


def test_collapse_keeps_rule_apart_and_takes_max_risk():
    from tzcheck.parse import ParsedDoc
    from tzcheck.pipeline import _collapse_repeats
    doc = ParsedDoc(name="x", blocks=[Block(id="p1", kind="para", text="срок поставки 10 дней", section=""),
                                      Block(id="p2", kind="para", text="срок поставки 10 дней", section="")],
                    fields={}, warnings=[], source_format="docx")
    a = Finding("p1", "срок поставки 10 дней", "Неполнота или комплектность", "x", "y", severity="low", rule_id="r.a")
    b = Finding("p2", "срок поставки 10 дней", "Неполнота или комплектность", "x", "y", severity="high", rule_id="r.a")
    c = Finding("p2", "срок поставки 10 дней", "Неполнота или комплектность", "x", "y", severity="low", rule_id="r.b")
    out = _collapse_repeats([a, b, c], doc)
    assert len(out) == 2 and out[0].severity == "high" and out[0].also_in == ["p2"]
