import json
import sys

import docx
import pytest

from conftest import fake_litellm
from tzcheck import learn_practice
from tzcheck.parse import parse


def _docx(path, paras, rows):
    d = docx.Document()
    for p in paras:
        d.add_paragraph(p)
    t = d.add_table(rows=len(rows), cols=len(rows[0]))
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            t.cell(i, j).text = cell
    d.save(str(path))
    return str(path)


HEAD = ["Наименование", "Характеристика", "Значение"]


@pytest.fixture
def pair(tmp_path):
    src = _docx(tmp_path / "src.docx",
                ["Техническое задание на поставку насосного оборудования",
                 "Товар должен быть марки Альфа или эквивалент по выбору заказчика.",
                 "Срок поставки товара составляет тридцать календарных дней с даты заключения договора."],
                [HEAD, ["Насос", "Напор", "не менее 10 м"], ["Насос", "Цвет корпуса", "синий"]])
    fin = _docx(tmp_path / "fin.docx",
                ["Техническое задание на поставку насосного оборудования",
                 "Товар должен соответствовать характеристикам, указанным в таблице ниже.",
                 "Срок поставки товара составляет тридцать календарных дней с даты заключения договора.",
                 "Приёмка товара проводится по акту приёма-передачи."],
                [HEAD, ["Насос", "Напор", "от 10 до 12 м"]])
    return src, fin


def test_diff_places_and_cells(pair):
    frags = learn_practice.diff(parse(pair[0]), parse(pair[1]))
    kinds = [f["kind"] for f in frags]
    assert kinds.count("исправлено") == 2 and kinds.count("добавлено") == 1 and kinds.count("удалено") == 1
    row = next(f for f in frags if f["cells"])
    assert row["cells"] == [("Значение", "не менее 10 м", "от 10 до 12 м")]
    para = next(f for f in frags if f["kind"] == "исправлено" and not f["cells"])
    assert "марки Альфа" in para["old"] and "таблице ниже" in para["new"]
    assert next(f for f in frags if f["kind"] == "удалено")["old"].startswith("Насос | Цвет корпуса")
    assert not any("Срок поставки" in f["old"] + f["new"] for f in frags)


def test_window_keeps_changed_words_without_repeats():
    old = " ".join(f"слово{i}" for i in range(200)) + " было"
    new = " ".join(f"слово{i}" for i in range(200)) + " стало"
    x, y = learn_practice._window(old, new)
    assert x.endswith("было") and y.endswith("стало") and "слово0 " not in x
    assert x.count("слово195") == 1


def _base(tmp_path):
    base = {"profiles": [
        {"id": "acme", "name": "АО Акме", "markers": ["Акме"], "verify_practice": ["СТАРАЯ ФРАЗА ПРОВЕРКИ"],
         "rules": {"old_rule": "СТАРОЕ ПРАВИЛО ПОИСКА"}, "extra": "СТАРОЕ ПОЛЕ"},
        {"id": "other", "name": "ООО Другое", "markers": ["Другое"], "rules": {"x": "правило другого заказчика"}}]}
    path = tmp_path / "customers.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return str(path), base


ANSWER = {"rules": [
    {"id": "Brand-Name", "category": "Ограничение конкуренции", "rule": "Не указывать марку товара: заменять марку требованиями к характеристикам."},
    {"id": "range", "category": "Некорректная характеристика", "rule": "Значение задавать диапазоном не менее 10 м."},
    {"id": "acts", "category": "Нет такой категории", "rule": "Порядок приёмки указывать в ТЗ."},
    {"id": "okpd", "category": "Неверный или общий код ОКПД 2", "rule": "Каждой позиции указывать свой код ОКПД 2 по 223-ФЗ."}],
    "verify_practice": ["Замечание о марке товара без характеристик заказчик считает верным.", "Срок не более 30 дней."]}


def test_cli_learns_profile_with_stub_model(pair, tmp_path, monkeypatch):
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return ANSWER

    monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm(answer))
    base_path, base = _base(tmp_path)
    out = tmp_path / "out" / "learned.json"
    code = learn_practice.main(["--pair", pair[0], pair[1], "--customer", "acme", "--profiles", base_path, "-o", str(out)])
    assert code == 0 and len(prompts) == 1
    p = prompts[0]
    assert p.startswith("Задача: практика заказчика")
    assert "было «не менее 10 м», стало «от 10 до 12 м»" in p and "марки Альфа" in p and "Приёмка товара" in p
    assert "Срок поставки" not in p
    assert "СТАРОЕ" not in p and "Акме" not in p
    data = json.loads(out.read_text(encoding="utf-8"))
    acme = next(x for x in data["profiles"] if x["id"] == "acme")
    assert set(acme) == {"id", "name", "markers", "verify_practice", "rules"}
    assert acme["name"] == "АО Акме" and acme["markers"] == ["Акме"]
    assert acme["rules"] == {"brand_name": "Не указывать марку товара: заменять марку требованиями к характеристикам. "
                                           "Категория замечания: «Ограничение конкуренции».",
                             "acts": "Порядок приёмки указывать в ТЗ.",
                             "okpd": "Каждой позиции указывать свой код ОКПД 2 по 223-ФЗ. Категория замечания: «Неверный или общий код ОКПД 2»."}
    assert acme["verify_practice"] == ["Замечание о марке товара без характеристик заказчик считает верным."]
    assert next(x for x in data["profiles"] if x["id"] == "other") == base["profiles"][1]


def test_several_batches_are_merged(pair, monkeypatch):
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return ANSWER

    monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm(answer))
    monkeypatch.setattr(learn_practice, "BATCH_CHARS", 200)
    learned, info = learn_practice.learn([pair])
    assert info["calls"] == len(prompts) > 2
    assert all(p.startswith("Задача: практика заказчика") for p in prompts[:-1])
    assert prompts[-1].startswith("Задача: свести практику")
    assert "правило [Ограничение конкуренции]: Не указывать марку товара" in prompts[-1]
    assert "brand_name" in learned["rules"]


def test_cli_without_model(pair, tmp_path, monkeypatch):
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)
    assert learn_practice.main(["--pair", pair[0], pair[1], "--customer", "acme", "-o", str(tmp_path / "x.json")]) == 2
    assert not (tmp_path / "x.json").exists()
