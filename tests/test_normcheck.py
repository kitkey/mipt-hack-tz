import json
import re
import zipfile
from types import SimpleNamespace

import docx

import tzcheck.rag as rag
from tzcheck.model import Finding
from tzcheck.pii import Scrubber
from tzcheck.rag import enrich, verify
from tzcheck.report import to_docx_comments, to_html

from test_rag import law_with_parts, write_corpus, _use_corpus
from test_rag_verify import TM_QUOTE, brand_finding

NORMCHECK = "Задача: проверить применимость пунктов норм"


class Model:
    """Заглушка модели: на запрос проверки применимости решение по каждой паре от answer(номер пары) или пустой ответ;
    отбор кандидатов (verify.judge) признаёт применимой ч. 6.1 статьи 3."""

    def __init__(self, answer=None):
        self.answer, self.prompts = answer, []

    def asked(self):
        return [p for p in self.prompts if p.startswith(NORMCHECK)]

    def __call__(self, prompt, max_tokens=0):
        self.prompts.append(prompt)
        if prompt.startswith(NORMCHECK):
            if self.answer is None:
                return {}
            data = prompt.split("<<<ТЗ>>>", 1)[1].split("<<<КОНЕЦ>>>", 1)[0]
            return {"decisions": [dict(n=int(n), **self.answer(int(n))) for n in re.findall(r"^(\d+)\. \[", data, re.M)]}
        out = []
        for line in prompt.splitlines():
            line = line.strip()
            if line.startswith(tuple("123456789")) and " (c=" in line:
                n, c = line.split(" ", 1)[0].split(".")
                ok = "ч. 6.1" in line
                out.append({"n": int(n), "c": int(c), "decision": "applicable" if ok else "not_applicable",
                            "quote": TM_QUOTE if ok else "", "reason": "товарный знак" if ok else "о другом"})
        return {"decisions": out}


def _doc():
    return SimpleNamespace(warnings=[], fields={}, text=lambda: "")


def _model_finding():
    """Замечание ядра (источник model:*): статью выбрала модель, пункт внутри статьи код подбирает без модели."""
    f = brand_finding()
    f.source, f.norm_ids = "model:find", ["223-FZ-3-6.1-2"]
    return f


def _run(monkeypatch, tmp_path, model, findings):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    monkeypatch.setattr("tzcheck.llm._call", model)
    doc = _doc()
    enrich(findings, doc=doc, scr=Scrubber(ner=False), use_llm=True)
    rag._searcher.cache_clear()
    return doc


def test_applicable_clause_stays_verified(monkeypatch, tmp_path):
    model = Model(lambda n: {"applicable": True, "reason": "запрет товарных знаков"})
    f = _model_finding()
    _run(monkeypatch, tmp_path, model, [f])
    assert len(model.asked()) == 1
    assert TM_QUOTE in model.asked()[0] and "<<<ТЗ>>>" in model.asked()[0]
    assert f.evidence["пункт нормы"].startswith("223-ФЗ, Статья 3, ч. 6.1")
    assert f.evidence[verify.BASIS] == verify.VERIFIED
    assert any(s["step"] == "norm" and "применим: запрет товарных знаков" in s["text"] for s in f.trace)


def test_not_applicable_clause_removed_finding_kept(monkeypatch, tmp_path):
    model = Model(lambda n: {"applicable": False, "reason": "пункт о другом"})
    f = _model_finding()
    findings = [f]
    _run(monkeypatch, tmp_path, model, findings)
    assert findings == [f]
    assert not {"пункт нормы", "цитата нормы", "источник нормы", verify.BASIS} & set(f.evidence)
    step = next(s for s in f.trace if s["step"] == "norm")
    assert step["text"].startswith("пункт 223-ФЗ, Статья 3, ч. 6.1") and "не применим: пункт о другом" in step["text"]


def test_rejected_clause_id_removed_from_norm_ids(monkeypatch, tmp_path):
    model = Model(lambda n: {"applicable": False, "reason": "пункт о другом"})
    f = _model_finding()
    f.norm_ids = ["223-FZ-3-6.1-2", "223-FZ-3-6-1", "TR-TS-004-5"]
    _run(monkeypatch, tmp_path, model, [f])
    assert f.norm_ids == ["223-FZ-3-6-1", "TR-TS-004-5"]
    assert "пункт нормы" not in f.evidence and verify.BASIS not in f.evidence
    step = next(s for s in f.trace if s["step"] == "norm")
    assert step["text"].endswith("снято из norm_ids: 223-FZ-3-6.1-2")
    f.finalize()
    assert f.evidence["основание из базы норм"] is True


def test_rejected_clause_last_id_leaves_no_basis(monkeypatch, tmp_path):
    model = Model(lambda n: {"applicable": False, "reason": "пункт о другом"})
    f = _model_finding()
    f.evidence["основание из базы норм"] = True
    f.evidence["нормы"] = ["223-ФЗ, ст. 3, ч. 6.1, п. 2"]
    _run(monkeypatch, tmp_path, model, [f])
    assert f.norm_ids == [] and f.evidence["основание из базы норм"] is False and "нормы" not in f.evidence


def test_no_answer_leaves_clause_marked_unchecked(monkeypatch, tmp_path):
    model = Model(None)
    f = _model_finding()
    _run(monkeypatch, tmp_path, model, [f])
    assert len(model.asked()) == 1
    assert f.evidence["пункт нормы"].startswith("223-ФЗ, Статья 3, ч. 6.1")
    assert f.evidence[verify.BASIS] == verify.NOT_CHECKED


def test_tz_text_masked_in_check_prompt(monkeypatch, tmp_path):
    model = Model(lambda n: {"applicable": True, "reason": "да"})
    f = _model_finding()
    f.quote = "Осциллограф RIGOL, звонить ivanov@example.org"
    _run(monkeypatch, tmp_path, model, [f])
    assert model.asked() and "ivanov@example.org" not in model.asked()[0]


def test_switch_off_skips_model(monkeypatch, tmp_path):
    monkeypatch.setenv("TZCHECK_NORMCHECK", "0")
    model = Model(lambda n: {"applicable": False, "reason": "нет"})
    f = _model_finding()
    _run(monkeypatch, tmp_path, model, [f])
    assert not model.asked()
    assert f.evidence["пункт нормы"] and f.evidence[verify.BASIS] == verify.NOT_CHECKED


def test_model_verified_clause_not_asked_again(monkeypatch, tmp_path):
    model = Model(lambda n: {"applicable": False, "reason": "нет"})
    f = brand_finding()  # без статьи: открытый поиск, кандидатов проверяет verify.judge
    _run(monkeypatch, tmp_path, model, [f])
    assert not model.asked()
    assert f.evidence["цитата нормы"] == TM_QUOTE and f.evidence[verify.BASIS] == verify.VERIFIED


def test_one_call_for_up_to_15_pairs_and_answer_per_pair(monkeypatch, tmp_path):
    model = Model(lambda n: {"applicable": n % 2 == 1, "reason": "по тексту"})
    findings = [_model_finding() for _ in range(16)]
    _run(monkeypatch, tmp_path, model, findings)
    assert len(model.asked()) == 2
    kept = [f for f in findings if "пункт нормы" in f.evidence]
    assert len(kept) == 9 and all(f.evidence[verify.BASIS] == verify.VERIFIED for f in kept)


def _result(ev):
    f = {"id": "F001", "block_id": "p1", "quote": "Осциллограф RIGOL DHO804 или эквивалент", "category": "Ограничение конкуренции",
         "severity": "high", "needs_review": False, "explanation": "Указан товарный знак.", "fix": "Убрать марку.", "norm_ids": [],
         "location": "абзац 1", "section": "main", "status": "ошибка", "evidence": ev}
    return {"_doc": SimpleNamespace(blocks=[]), "findings": [f], "norms": {}, "stats": {}}


def _clause(mark):
    return {"пункт нормы": "223-ФЗ, Статья 3, ч. 6.1", "цитата нормы": TM_QUOTE, verify.BASIS: mark}


def test_report_and_word_show_unchecked_mark(tmp_path):
    d = docx.Document()
    d.add_paragraph("Осциллограф RIGOL DHO804 или эквивалент")
    src, out = tmp_path / "tz.docx", tmp_path / "out.docx"
    d.save(src)
    res = dict(_result(_clause(verify.NOT_CHECKED)), _paths=[str(src)])
    assert to_docx_comments(res, str(src), str(out))
    with zipfile.ZipFile(out) as z:
        comments = z.read("word/comments.xml").decode("utf-8")
    assert "Пункт нормы 223-ФЗ, Статья 3, ч. 6.1 (применимость не проверена)" in comments
    page = to_html(res)
    assert "(применимость не проверена)" in page and "основание пункта" not in page
    assert "(проверено моделью)" in to_html(_result(_clause(verify.VERIFIED)))
    assert "применимость не проверена" not in to_html(_result(_clause(verify.VERIFIED)))
    empty = to_html(_result({}))
    assert verify.NOT_CHECKED not in empty and verify.VERIFIED not in empty


def test_word_comment_marks_verified_clause(tmp_path):
    d = docx.Document()
    d.add_paragraph("Осциллограф RIGOL DHO804 или эквивалент")
    src, out = tmp_path / "tz.docx", tmp_path / "out.docx"
    d.save(src)
    res = dict(_result(_clause(verify.VERIFIED)), _paths=[str(src)])
    assert to_docx_comments(res, str(src), str(out))
    with zipfile.ZipFile(out) as z:
        comments = z.read("word/comments.xml").decode("utf-8")
    assert "(проверено моделью)" in comments and "применимость не проверена" not in comments
    assert json.dumps(res["findings"][0]["evidence"], ensure_ascii=False).count(verify.VERIFIED) == 1
