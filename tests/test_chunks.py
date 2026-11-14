"""Текст ТЗ доходит до модели целиком: длинная часть делится по границам, а не срезается; короткая выдержка берётся вокруг слов запроса."""
import json
import re
import sys

import pytest
from conftest import fake_litellm

from tzcheck import chunks, core, gost_check, passport, pipeline, tables
from tzcheck.parse import Block, ParsedDoc
from tzcheck.pii import Scrubber

CATS = list(core.CATEGORIES)


def words(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:05d}" for i in range(n)]


def sentences(tokens: list[str], per: int = 12, lead: str = "") -> str:
    return " ".join(lead + " ".join(tokens[i:i + per]) + "." for i in range(0, len(tokens), per))


@pytest.mark.parametrize("limit", [40, 300, 5000])
def test_split_loses_nothing_and_keeps_limit(limit):
    text = "Первая строка требований.\nВторая строка; с уточнением: значение.\n" * 40 + "x" * 700 + " хвост без точки"
    parts = chunks.split(text, limit)
    assert "".join(parts) == text and max(len(p) for p in parts) <= limit


def test_split_cuts_at_line_then_sentence_then_space():
    text = "строка один\nстрока два. фраза три; часть четыре: пять шесть"
    assert chunks.split(text, 30) == ["строка один\n", "строка два. фраза три; ", "часть четыре: пять шесть"]
    assert chunks.split("фраза раз. фраза два. фраза три.", 14) == ["фраза раз. ", "фраза два. ", "фраза три."]
    assert chunks.split("слово " * 5, 13) == ["слово слово ", "слово слово ", "слово "]


def test_lines_repeat_block_id_on_every_piece():
    line = "[t1r5] " + sentences(words("v", 120))
    parts = chunks.lines(line, 300)
    assert len(parts) > 3 and all(p.startswith("[t1r5] ") and len(p) <= 300 for p in parts)
    assert set(re.findall(r"v\d{5}", " ".join(parts))) == set(words("v", 120))


def test_window_shows_place_of_quote_and_of_query_words():
    text = sentences(words("a", 300)) + " Гарантийный срок на товар составляет 24 месяца со дня приёмки. " + sentences(words("b", 300))
    assert "24 месяца" in chunks.window(text, "Гарантийный срок на товар составляет 24 месяца", 200)
    cut = chunks.window(text, "гарантийного срока товара", 200)
    assert "24 месяца" in cut and len(cut) <= 202 and cut.startswith("…") and cut.endswith("…")
    assert chunks.window("короткий текст", "нет", 200) == "короткий текст"


class Stub:
    def __init__(self, answers):
        self.answers, self.prompts = answers, []

    def __call__(self, prompt, max_tokens=4000):
        self.prompts.append(prompt)
        for key, ans in self.answers.items():
            if prompt.startswith("Задача: " + key):
                return ans(prompt) if callable(ans) else json.loads(json.dumps(ans))
        return {}

    def asked(self, key):
        return [p for p in self.prompts if p.startswith("Задача: " + key)]


@pytest.fixture
def stub(monkeypatch):
    def make(answers):
        s = Stub(answers)
        monkeypatch.setitem(sys.modules, "litellm", fake_litellm(s))
        monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
        return s
    return make


def _row(bid, row, cells, vmerge=None):
    vm = vmerge or [False] * len(cells)
    return Block(bid, "row", " | ".join(c for c, v in zip(cells, vm) if c and not v), "Спецификация", 0, row, list(cells), grid=list(cells),
                 hmerge=[False] * len(cells), vmerge=vm)


HEADER = {"header_rows": 1, "kind": "positions", "item": "",
          "columns": [{"index": 0, "role": "num", "label": "№"}, {"index": 1, "role": "name", "label": "Наименование"},
                      {"index": 2, "role": "char_name", "label": "Характеристика"}, {"index": 3, "role": "value", "label": "Значение"}]}


def long_doc():
    """Абзац втрое длиннее части, позиция из 60 строк и строка с ячейкой длиннее части; у каждого слова свой номер."""
    blocks = [Block("h1", "heading", "Техническое задание", "Техническое задание"),
              Block("p1", "paragraph", sentences(words("p", 2000), lead="Поставщик обязан "), "Требования"),
              Block("p2", "paragraph", "Поставщик поставляет товар за 30 дней " + sentences(words("s", 40)), "Требования"),
              _row("t1r1", 0, ["№", "Наименование", "Характеристика", "Значение"])]
    for i in range(60):
        blocks.append(_row(f"t1r{i + 2}", i + 1, ["1", "Анализатор", f"Параметр {i}", sentences(words(f"r{i:02d}x", 20), lead="Анализатор даёт ")],
                           vmerge=[i > 0, i > 0, False, False]))
    blocks.append(_row("t1r62", 61, ["1", "Анализатор", "Описание", sentences(words("c", 900), lead="Анализатор даёт ")],
                       vmerge=[True, True, False, False]))
    blocks.append(Block("c1", "paragraph", "Поставщик поставляет анализатор по договору за 45 дней.", "Проект договора", source="договор.docx"))
    return ParsedDoc("tz.docx", blocks, {}, [], "docx")


ROLES = {"passport": {"law": "223-ФЗ", "method": "", "competitive": True, "kind": "товар", "subject": "анализатор", "customer": "",
                      "customer_profile": ""},
         "roles": [{"id": "h1", "role": "req"}, {"id": "t1r1", "role": "spec"}, {"id": "c1", "role": "contract"}]}


def _prep(stub):
    s = stub({"паспорт": ROLES, "разметка шапки": HEADER})
    doc = long_doc()
    pipeline.start_core(doc)
    passport.detect(doc, Scrubber())
    tables.build(doc, Scrubber())
    return s, doc


def _own_words(doc) -> set:
    """Номерные слова проверяемых блоков (без проекта договора и строки шапки)."""
    return {w for b in doc.blocks if b.source == "" and b.id != "t1r1" for w in re.findall(r"[a-z]\d*x?\d{5}", b.text)}


def test_every_block_of_every_unit_reaches_find_prompt(stub):
    s, doc = _prep(stub)
    core.find(doc, Scrubber(), CATS)
    prompts = s.asked("найти дефекты")
    data = [p.split("<<<ТЗ>>>")[-1] for p in prompts]
    assert max(len(d) for d in data) <= core.UNIT_CHARS * 1.5
    seen = set(re.findall(r"[a-z]\d*x?\d{5}", " ".join(data)))
    assert _own_words(doc) <= seen
    for u in doc.core_units:
        # у каждой части в запросе стоят строки именно её блоков
        marks = set(re.findall(r"^\[([^\]\s]+)\]", u.text, re.M))
        assert marks and marks <= set(u.block_ids)
    # кусок длинной строки таблицы несёт её [id]: цитата из любого куска привязывается к блоку
    assert sum("[t1r62] " in d for d in data) >= 2 and sum("[p1] " in d for d in data) >= 3


def test_every_block_of_unit_reaches_cross_prompt(stub):
    s, doc = _prep(stub)
    core.find(doc, Scrubber(), CATS)
    core.cross(doc, Scrubber(), CATS)
    prompts = s.asked("сверка частей")
    assert prompts
    # у каждой части есть место в договоре, поэтому на сверку идут все; часть идёт в запрос целиком, а не первыми 3500 символами
    assert all("[c1]" in p for p in prompts)
    assert _own_words(doc) <= set(re.findall(r"[a-z]\d*x?\d{5}", " ".join(prompts)))


def test_gost_target_text_goes_whole_in_parts():
    text = "Позиция 1: Лифт\n" + "\n".join(f"[t1r{i}] Параметр {i}: " + sentences(words(f"g{i:02d}x", 40)) for i in range(80))
    parts = gost_check._texts(text)
    assert len(parts) > 1 and all(p.startswith("Позиция 1: Лифт\n") and len(p) <= gost_check.TEXT_CHARS + 20 for p in parts)
    assert set(re.findall(r"g\d\dx\d{5}", " ".join(parts))) == set(re.findall(r"g\d\dx\d{5}", text))
    assert gost_check._texts("Позиция 1: Лифт\n[t1r1] Скорость: 1 м/с") == ["Позиция 1: Лифт\n[t1r1] Скорость: 1 м/с"]
