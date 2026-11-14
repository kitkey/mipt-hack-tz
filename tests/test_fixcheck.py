"""Проверка правки (tzcheck.fixcheck): замену цитаты модель проверяет отдельным вызовом до правок Word. Подтверждённая замена
идёт в правки, неподтверждённая нет (замечание и комментарий остаются), без ответа модели всё как раньше, TZCHECK_FIXCHECK=0
шаг выключает."""
import json
import os
import re
import sys

import docx
import pytest
from conftest import fake_litellm

from tzcheck import feedback, fixcheck, pipeline, report, revisions
from tzcheck.model import Finding
from tzcheck.parse import Block, ParsedDoc
from tzcheck.pii import Scrubber

KEY = "проверить предложенные правки"
PASSPORT = {"passport": {"law": "223-ФЗ", "method": "запрос котировок", "competitive": True, "kind": "товар", "subject": "поставка",
                         "customer": "Заказчик", "customer_profile": ""}, "roles": []}
FINDING = {"block_id": "p1", "quote": "в разумный срок", "category": "Некорректная характеристика", "severity": "high",
           "explanation": "Срок не определён.", "fix": "Указать число дней.", "replacement": "в течение 10 рабочих дней", "confidence": 90}
KEEP = {"decisions": [{"n": 0, "decision": "keep", "reason": "срок не задан", "confidence": 95}]}
REPLACEMENT = "в течение 10 рабочих дней"


class Stub:
    """Заглушка модели: ответ по началу задачи в запросе, запросы сохраняются."""

    def __init__(self, answers):
        self.answers, self.prompts = answers, []

    def __call__(self, prompt, max_tokens=4000):
        self.prompts.append(prompt)
        if prompt.startswith("По результатам проверки"):
            return {"summary": "Заключение заглушки."}
        for key, ans in self.answers.items():
            if prompt.startswith("Задача: " + key):
                return ans(prompt) if callable(ans) else json.loads(json.dumps(ans))
        return {}

    def asked(self, key):
        return [p for p in self.prompts if p.startswith("Задача: " + key)]


def numbers(prompt: str) -> list[int]:
    return [int(n) for n in re.findall(r"^(\d+)\. \[", prompt.split("<<<ТЗ>>>", 1)[-1], re.M)]


def answer(ok: bool, reason: str):
    return lambda p: {"decisions": [{"n": n, "ok": ok, "reason": reason} for n in numbers(p)]}


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Проверка docx с одним замечанием и заменой; fixcheck: ответ шага проверки правок (None: модель не ответила)."""
    monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
    monkeypatch.setenv("TZCHECK_LLM_WORKERS", "1")
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    for name in ("TZCHECK_RAG", "TZCHECK_FIXCHECK", "TZCHECK_FIXCHECK_MODEL", "TZCHECK_ORCHESTRATOR"):
        monkeypatch.delenv(name, raising=False)
    d = docx.Document()
    d.add_paragraph("Срок поставки: в разумный срок.")
    d.add_paragraph("Поставка в рабочие дни, телефон ответственного +7 (495) 123-45-67, приёмка по акту.")
    path = tmp_path / "tz.docx"
    d.save(path)

    def go(fix_answer, orch="linear"):
        answers = {"паспорт": PASSPORT, "найти дефекты": {"findings": [FINDING]}, "независимая вторая проверка": KEEP}
        if fix_answer is not None:
            answers[KEY] = fix_answer
        s = Stub(answers)
        monkeypatch.setitem(sys.modules, "litellm", fake_litellm(s))
        return s, pipeline.check(str(path), use_llm=True, orchestrator=orch), str(path)
    return go


def _step(f: dict) -> dict | None:
    return next((x for x in f["trace"] if x["step"] == "fixcheck"), None)


@pytest.mark.parametrize("orch", ["linear", "graph"])
def test_confirmed_fix_goes_to_word_revisions(run, tmp_path, orch):
    s, r, path = run(answer(True, "срок задан числом"), orch)
    f = r["findings"][0]
    assert (f["section"], f["replacement"]) == ("main", REPLACEMENT)
    assert _step(f)["text"] == "правку модель подтвердила" and _step(f)["call"]
    assert [x["step"] for x in f["trace"]][-2:] == ["fixcheck", "final"] and REPLACEMENT in f["trace"][-1]["text"]
    assert r["stats"]["revisions"] == 1 and r["model_calls"][fixcheck.STEP] == 1
    assert revisions.write(r, path, str(tmp_path))["main"]["applied"] == [f["id"]]
    prompts = s.asked(KEY)
    assert len(prompts) == 1
    p = prompts[0]
    assert numbers(p) == [1] and "Правок в запросе: 1, их номера: 1." in p
    for text in ("«в разумный срок»", "Срок не определён.", "Указать число дней.", f"Предлагаемая замена: «{REPLACEMENT}»", "» [p1]"):
        assert text in p
    # соседний абзац виден модели, ПД в нём под той же маской, что и в других шагах
    assert "[p2]" in p and "123-45-67" not in p and "[PHONE_1]" in p


def test_unconfirmed_fix_not_in_revisions_comment_kept(run, tmp_path):
    s, r, path = run(answer(False, "срок расходится с соседним абзацем"))
    f = r["findings"][0]
    assert (f["section"], f["status"], f["replacement"], f["explanation"]) == ("main", "ошибка", "", "Срок не определён.")
    assert _step(f)["text"] == "правку модель не подтвердила: срок расходится с соседним абзацем"
    assert REPLACEMENT in f["evidence"]["проверка правки"] and REPLACEMENT not in f["trace"][-1]["text"]
    assert r["stats"]["revisions"] == 0 and r["status"] == "ok"
    assert revisions.write(r, path, str(tmp_path)) == {}
    out = tmp_path / "tz.comments.docx"
    assert report.to_docx_comments(r, path, str(out))
    assert any(f"[{f['id']}]" in c.text and "Срок не определён." in c.text for c in docx.Document(out).comments)


@pytest.mark.parametrize("fix_answer", [None, {"decisions": []}, {"decisions": [{"n": 1, "reason": "без поля ok"}, {"n": 7, "ok": False}]}])
def test_no_model_answer_changes_nothing(run, monkeypatch, fix_answer):
    s, r, _ = run(fix_answer)
    assert len(s.asked(KEY)) == 1
    monkeypatch.setenv("TZCHECK_FIXCHECK", "0")
    s0, r0, _ = run(fix_answer)
    assert s0.asked(KEY) == []
    keys = ("id", "quote", "section", "status", "severity", "replacement", "evidence", "explanation", "fix")
    assert [{k: f[k] for k in keys} for f in r["findings"]] == [{k: f[k] for k in keys} for f in r0["findings"]]
    assert [[(x["step"], x.get("text")) for x in f["trace"]] for f in r["findings"]] == \
           [[(x["step"], x.get("text")) for x in f["trace"]] for f in r0["findings"]]
    assert r["findings"][0]["replacement"] == REPLACEMENT and _step(r["findings"][0]) is None
    assert (r["status"], r["warnings"], r["summary"], r["stats"]["revisions"]) == (r0["status"], r0["warnings"], r0["summary"], 1)


def test_switch_off_makes_no_call(run, monkeypatch):
    monkeypatch.setenv("TZCHECK_FIXCHECK", "0")
    s, r, _ = run(answer(False, "не устраняет"))
    assert s.asked(KEY) == [] and fixcheck.STEP not in r["model_calls"]
    f = r["findings"][0]
    assert f["replacement"] == REPLACEMENT and _step(f) is None and "проверка правки" not in f["evidence"]


def test_step_can_use_its_own_model(run, monkeypatch):
    monkeypatch.setenv("TZCHECK_FIXCHECK_MODEL", "stub/fixcheck")
    s, r, _ = run(answer(True, "ок"))
    calls = r["_trace"]["calls"]
    assert [c["model"] for c in calls if c["stage"] == fixcheck.STAGE] == ["stub/fixcheck"]
    assert {c["model"] for c in calls if c["stage"] != fixcheck.STAGE} == {"stub/model"}


def test_batches_of_twenty_and_only_main_with_replacement(monkeypatch):
    monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
    monkeypatch.delenv("TZCHECK_FIXCHECK", raising=False)
    monkeypatch.delenv("TZCHECK_FIXCHECK_MODEL", raising=False)
    blocks = [Block(f"p{i}", "paragraph", f"Пункт {i}. Срок поставки: в разумный срок.", "Условия поставки") for i in range(1, 24)]
    doc = ParsedDoc("tz.docx", blocks, {}, [], "docx")
    pipeline.start_core(doc)
    fs = [Finding(b.id, "в разумный срок", "Некорректная характеристика", "Срок не определён.", "Указать число дней.",
                  replacement=REPLACEMENT) for b in blocks]
    fs[0].replacement = ""
    fs[1].section = "llm_review"
    s = Stub({KEY: lambda p: {"decisions": [{"n": n, "ok": n % 2 == 0, "reason": "по тексту"} for n in numbers(p)]}})
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm(s))
    checked = fixcheck.apply(doc, Scrubber(), fs)
    assert checked == fs[2:] and [len(numbers(p)) for p in s.asked(KEY)] == [20, 1]
    assert [bool(f.replacement) for f in checked] == [n % 2 == 0 for n in range(1, 22)]
    assert fs[0].trace == [] and fs[1].trace == [] and fs[1].replacement == REPLACEMENT


def test_lift_sample_only_confirmed_fixes_reach_word(tmp_path, scripted, monkeypatch):
    """Образец ТЗ на заглушке ядра: в файл правок Word идут только замены, которые проверка правки подтвердила."""
    monkeypatch.setattr(feedback, "PATH", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    monkeypatch.delenv("TZCHECK_FIXCHECK", raising=False)

    def model(prompt, max_tokens=4000):
        if prompt.startswith("Задача: " + KEY):
            scripted.prompts.append(prompt)
            return {"decisions": [{"n": n, "ok": n % 2 == 1, "reason": "по тексту"} for n in numbers(prompt)]}
        return scripted(prompt, max_tokens)

    monkeypatch.setitem(sys.modules, "litellm", fake_litellm(model))
    lift = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "lift_src.docx")
    r = pipeline.check(lift, use_llm=True)
    checked = [f for f in r["findings"] if _step(f)]
    kept = {f["id"] for f in checked if f["replacement"]}
    dropped = {f["id"] for f in checked if not f["replacement"]}
    assert kept and dropped and len(scripted.asked(KEY)) == -(-len(checked) // fixcheck.BATCH)
    assert all(f["section"] == "main" for f in checked) and r["stats"]["revisions"] == len(kept)
    out = revisions.write(r, lift, str(tmp_path))["main"]
    touched = {x.split(":")[0] for x in out["applied"]} | set(out["skipped"])
    assert touched == kept and not touched & dropped
