import json

import numpy as np
import pytest

from tzcheck import gost_profiles as gp
from tzcheck.model import SECTION_UNCHECKED, Finding
from tzcheck.parse import Block, ParsedDoc


def standard():
    return {"id": "gost-99999-2020", "document": "ГОСТ 99999-2020", "edition": "2020", "source": "https://example.org/g", "blocks": [
        {"type": "text", "text": "1 РАЗРАБОТАН Тестовым комитетом"},
        {"type": "text", "text": "Введение"},
        {"type": "text", "text": "При заключении договора на поставку лифта заказчик и поставщик согласовывают:"},
        {"type": "text", "text": "а) назначение и предполагаемое использование лифта;"},
        {"type": "text", "text": "в) условия окружающей среды (температура, влажность и т.п.);"},
        {"type": "text", "text": "1 Область применения"},
        {"type": "text", "text": "Настоящий стандарт распространяется на лифты."},
        {"type": "text", "text": "2 Требования"},
        {"type": "text", "text": "2.1 Номинальная скорость лифта должна быть не более 1,0 м/с."},
        {"type": "text", "text": "1 Примечание к пункту без нового номера."},
        {"type": "text", "text": "2.2 Время работы от резервного источника питания должно быть не менее 60 минут."},
        {"type": "table", "text": "Параметр | Значение\nГлубина приямка, мм | не менее 1000"},
        {"type": "text", "text": "3 Документация"},
        {"type": "text", "text": "3.1 Каждый лифт комплектуется паспортом и руководством по эксплуатации."},
        {"type": "text", "text": "Приложение А (обязательное) Форма паспорта"},
        {"type": "text", "text": "А.1 Паспорт содержит сведения о лифте."}]}


def model_items():
    return {"items": [
        {"kind": "order_param", "clause": "Введение", "quote": "условия окружающей среды (температура, влажность и т.п.)", "trigger": "заказчик и поставщик согласовывают",
         "param": "условия окружающей среды", "aliases": ["температура эксплуатации", "климатическое исполнение"], "requirement": "Согласовать условия окружающей среды.",
         "applies_to": "лифт", "condition": ""},
        {"kind": "order_param", "clause": "Введение", "quote": "назначение и предполагаемое использование лифта", "trigger": "заказчик и поставщик согласовывают",
         "param": "назначение лифта", "aliases": ["тип лифта"], "requirement": "Указать назначение лифта.", "applies_to": "лифт", "condition": ""},
        {"kind": "limit", "clause": "2.1", "quote": "Номинальная скорость лифта должна быть не более 1,0 м/с", "param": "скорость", "aliases": ["скорость движения кабины"],
         "requirement": "Скорость не более 1 м/с.", "applies_to": "лифт", "condition": "", "limit": {"op": "max", "min": None, "max": 1.0, "unit": "м/с"}},
        {"kind": "limit", "clause": "2.2", "quote": "Время работы от резервного источника питания должно быть не менее 60 минут",
         "param": "время работы от резервного питания", "aliases": [], "requirement": "Не менее 60 минут.", "applies_to": "лифт", "condition": "",
         "limit": {"op": "min", "min": 60, "max": None, "unit": "минут"}},
        {"kind": "docs", "clause": "3.1", "quote": "Каждый лифт комплектуется паспортом", "param": "паспорт лифта", "aliases": ["паспорт"],
         "requirement": "Передать паспорт.", "applies_to": "лифт", "condition": ""},
        {"kind": "docs", "clause": "3.1", "quote": "руководством по эксплуатации", "param": "руководство по эксплуатации", "aliases": [],
         "requirement": "Передать руководство.", "applies_to": "лифт", "condition": ""},
        # отбрасываются кодом
        {"kind": "limit", "clause": "2.1", "quote": "Номинальная скорость лифта должна быть не более 1,6 м/с", "param": "скорость", "limit": {"op": "max", "max": 1.6, "unit": "м/с"}},
        {"kind": "limit", "clause": "2.1", "quote": "Номинальная скорость лифта должна быть не более 1,0 м/с", "param": "скорость", "limit": {"op": "max", "max": 2.0, "unit": "м/с"}},
        {"kind": "docs", "clause": "3.1", "quote": "Каждый лифт комплектуется сертификатом соответствия", "param": "сертификат"},
        {"kind": "order_param", "clause": "2.1", "quote": "Номинальная скорость лифта должна быть не более 1,0 м/с", "trigger": "должна быть", "param": "скорость"},
        {"kind": "method", "clause": "2.1", "quote": "Номинальная скорость лифта", "param": "скорость"},
    ]}


def test_segments_skip_preface_and_notes():
    segs = gp.segments(standard())
    clauses = [s["clause"] for s in segs]
    assert clauses == ["Введение", "1", "2", "2.1", "2.2", "3", "3.1", "Приложение А", "А.1"]
    s51 = next(s for s in segs if s["clause"] == "2.1")
    assert "Примечание к пункту" in s51["text"]
    assert next(s for s in segs if s["clause"] == "2.2")["tables"]


def test_locate_quote_exact_fuzzy_and_missing():
    segs = gp.segments(standard())
    exact = gp.locate_quote("номинальная  скорость лифта должна быть не более 1,0 м/с", segs)
    assert exact["match"] == "exact" and exact["clause"] == "2.1" and exact["quote"].startswith("Номинальная")
    fuzzy = gp.locate_quote("Номинальная скорость лифтов должна быть не более 1,0 м/с", segs)
    assert fuzzy and fuzzy["match"] == "fuzzy" and fuzzy["quote"] in segs[3]["text"]
    assert gp.locate_quote("Лифт оборудуют системой видеонаблюдения кабины", segs) is None


def test_validate_items_keeps_checked_and_drops_rest():
    segs = gp.segments(standard())
    clauses = gp.clause_set(segs)
    kept, reasons = [], []
    for raw in model_items()["items"]:
        it, why = gp.validate_item(raw, segs, clauses)
        (kept.append(it) if it else reasons.append(why))
    assert [i["kind"] for i in kept] == ["order_param", "order_param", "limit", "limit", "docs", "docs"]
    assert kept[3]["limit"] == {"op": "min", "min": 60.0, "max": None, "values": [], "unit": "мин"}
    assert any("1.6" in r or "1,6" in r or "цитаты нет" in r for r in reasons)
    assert any("числа 2 нет" in r for r in reasons)
    assert any("цитаты нет" in r for r in reasons)
    assert any("не про заказчика" in r for r in reasons)
    assert any("не из списка" in r for r in reasons)


def test_wrong_clause_is_corrected_to_where_quote_stands():
    segs = gp.segments(standard())
    it, _ = gp.validate_item({"kind": "docs", "clause": "9.9", "quote": "Каждый лифт комплектуется паспортом", "param": "паспорт"}, segs, gp.clause_set(segs))
    assert it["clause"] == "3.1" and "не найден" in it["clause_note"]


def test_build_without_model_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_GOST_PROFILES", str(tmp_path))
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)
    monkeypatch.setattr(gp, "_texts", lambda: {"ГОСТ 99999-2020": (standard(), "x")})
    assert gp.build("ГОСТ 99999-2020") is None


def fake_embed(texts):
    """Детерминированные векторы: одинаковые параметры почти совпадают, разные ортогональны."""
    vocab = {}
    out = []
    for t in texts:
        key = t.split(":")[0].strip().lower()
        k = vocab.setdefault(key, len(vocab))
        v = np.zeros(64)
        v[k % 64] = 1.0
        v[(k + 1) % 64] = 0.05 * (len(t) % 3)
        out.append(v / np.linalg.norm(v))
    return np.array(out)


def built(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_GOST_PROFILES", str(tmp_path))
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)
    monkeypatch.setattr(gp, "_texts", lambda: {"ГОСТ 99999-2020": (standard(), "x")})
    monkeypatch.setattr(gp, "_embed", fake_embed)
    prof = gp.build("ГОСТ 99999-2020", complete=lambda prompt: model_items())
    return prof


def test_build_caches_profile_and_reuses_without_model(tmp_path, monkeypatch):
    prof = built(tmp_path, monkeypatch)
    assert prof["counts"] == {"order_param": 2, "limit": 2, "docs": 2}
    assert (tmp_path / "gost-99999-2020.json").exists() and (tmp_path / "_canonical.json").exists()
    again = gp.build("ГОСТ 99999-2020", complete=lambda prompt: pytest.fail("модель не должна вызываться"))
    assert again["built_at"] == prof["built_at"]


def item(i, kind, param, req, lim=None, d="ГОСТ 1-2020"):
    return {"id": f"x#{i}", "kind": kind, "param": param, "requirement": req, "quote": req, "clause": str(i), "aliases": [], "limit": lim}


def test_merge_thresholds_model_and_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_GOST_PROFILES", str(tmp_path))
    a = {"designation": "ГОСТ 1-2020", "slug": "a", "text_sha256": "1", "items": [
        item(1, "docs", "паспорт", "Передать паспорт лифта."),
        item(2, "limit", "скорость", "Скорость не более 1 м/с.", {"op": "max", "min": None, "max": 1.0, "unit": "м/с", "values": []})]}
    b = {"designation": "ГОСТ 2-2021", "slug": "b", "text_sha256": "2", "items": [
        item(3, "docs", "паспорт", "Лифт комплектуется паспортом."),
        item(4, "limit", "скорость", "Скорость не более 1,6 м/с.", {"op": "max", "min": None, "max": 1.6, "unit": "м/с", "values": []}),
        item(5, "docs", "руководство", "Передать руководство по эксплуатации.")]}

    def embed(texts):
        v = fake_embed(texts)
        return v

    asked = []

    def judge(prompt):
        asked.append(prompt)
        return {"decisions": [{"pair": 1, "same": True, "why": "одно и то же"}]}

    out = gp.merge([a, b], judge=judge, embed=embed)
    by_param = {}
    for c in out["items"]:
        by_param.setdefault(c["param"], []).append(c)
    assert len(by_param["паспорт"]) == 1 and len(by_param["паспорт"][0]["sources"]) == 2
    assert len(by_param["скорость"]) == 2
    assert any(d["decision"] == "не слито" and "разные" in d["by"] for d in out["decisions"])


def test_merge_gray_zone_goes_to_model(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_GOST_PROFILES", str(tmp_path))
    p = {"designation": "ГОСТ 1-2020", "slug": "a", "text_sha256": "1", "items": [
        item(1, "docs", "паспорт", "Передать паспорт."), item(2, "docs", "формуляр", "Передать формуляр.")]}

    def embed(texts):
        v = np.zeros((len(texts), 4))
        v[0] = [1, 0, 0, 0]
        v[1] = [0.85, np.sqrt(1 - 0.85 ** 2), 0, 0]
        for k in range(2, len(texts)):
            v[k] = [0, 0, 0, 1]
        return v

    out = gp.merge([p], judge=lambda prompt: {"decisions": [{"pair": 1, "same": False, "why": "разные документы"}]}, embed=embed)
    assert out["canonical_total"] == 2
    assert out["decisions"][0]["by"] == "модель" and out["decisions"][0]["decision"] == "не слито"
    # без модели прошлый ответ модели переиспользуется
    out = gp.merge([p], judge=None, embed=embed)
    assert out["decisions"][0]["why"] == "разные документы"
    (tmp_path / "_canonical.json").unlink()
    out = gp.merge([p], judge=None, embed=embed)
    assert out["canonical_total"] == 2 and "модель выключена" in out["decisions"][0]["why"]


def test_rule_topics_mark_covered(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_GOST_PROFILES", str(tmp_path))
    p = {"designation": "ГОСТ 1-2020", "slug": "a", "text_sha256": "1", "items": [item(1, "order_param", "гарантийный срок", "Указать гарантийный срок.")]}

    def embed(texts):
        v = np.zeros((len(texts), 3))
        v[0] = [1, 0, 0]
        for k in range(1, len(texts)):
            v[k] = [0, 1, 0]
        v[1 + list(gp.RULE_TOPICS).index("warranty")] = [1, 0, 0]
        return v

    out = gp.merge([p], judge=None, embed=embed)
    assert out["items"][0]["covered_by_rule"]["rule"] == "warranty"
    assert gp.lexicon_records(out["items"])[0]["status"] == "retired"


def doc_of(rows: list[tuple[str, str]]) -> ParsedDoc:
    blocks = []
    for i, (kind, text) in enumerate(rows):
        if kind == "row":
            blocks.append(Block(f"t1r{i}", "row", text, "Характеристики", 0, i, [c.strip() for c in text.split("|")]))
        else:
            blocks.append(Block(f"p{i}", "para", text, "Раздел"))
    return ParsedDoc("tz.docx", blocks, {}, [], "docx")


def test_apply_finds_missing_params_limit_and_docs(tmp_path, monkeypatch):
    built(tmp_path, monkeypatch)
    doc = doc_of([("para", "Техническое задание на замену лифта"),
                  ("para", "Лифт должен соответствовать ГОСТ 99999-2020 и ГОСТ 12345-2019."),
                  ("row", "Скорость, м/с | 1,6"),
                  ("row", "Тип лифта | пассажирский"),
                  ("para", "Подрядчик передаёт Заказчику исполнительную документацию, в которую входит паспорт лифта.")])
    out = gp.apply(doc, [])
    by_rule = {}
    for f in out:
        by_rule.setdefault(f.rule_id, []).append(f)
    lim = by_rule["gost_profile.limit"][0]
    assert lim.category == "Противоречие" and lim.block_id == "t1r2" and "1,6" in lim.quote
    assert any("ГОСТ 99999-2020, п. 2.1" in x for x in lim.evidence["основания по стандартам"])
    order = by_rule["gost_profile.order_param"][0]
    assert order.block_id == "p1" and "условия окружающей среды" in order.explanation and "назначение" not in order.explanation
    assert order.status == "неясно" and order.severity == "medium"
    docs = by_rule["gost_profile.docs"][0]
    assert "руководство по эксплуатации" in docs.explanation and "паспорт лифта:" not in docs.explanation
    unavailable = by_rule["gost_profile.unavailable"][0]
    assert unavailable.section == SECTION_UNCHECKED and "ГОСТ 12345-2019" in unavailable.explanation


def test_apply_reports_not_built_without_model(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_GOST_PROFILES", str(tmp_path))
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)
    monkeypatch.setattr(gp, "_texts", lambda: {"ГОСТ 99999-2020": (standard(), "x")})
    doc = doc_of([("para", "ТЗ"), ("para", "Лифт по ГОСТ 99999-2020.")])
    out = gp.apply(doc, [], build_missing=True)
    assert len(out) == 1 and out[0].rule_id == "gost_profile.not_built" and out[0].section == SECTION_UNCHECKED
    assert "модель выключена" in out[0].explanation


def test_same_place_same_category_is_merged_into_existing(tmp_path, monkeypatch):
    built(tmp_path, monkeypatch)
    doc = doc_of([("para", "ТЗ"), ("para", "Лифт по ГОСТ 99999-2020, назначение лифта: грузовой, условия окружающей среды: от 5 до 40 °C, паспорт, руководство по эксплуатации."),
                  ("row", "Скорость, м/с | 1,6")])
    existing = Finding("t1r2", "1,6", "Противоречие", "Скорость в таблице не совпадает с текстом.", "Исправить.")
    out = gp.apply(doc, [existing])
    assert len(out) == 1 and out[0] is existing
    assert any("п. 2.1" in x for x in existing.evidence["основания по стандартам"])


def test_r_prefix_error_uses_existing_text(monkeypatch):
    monkeypatch.setattr(gp, "_exists", lambda d: False)
    found = gp.find_text("ГОСТ Р 99999-2020", {"ГОСТ 99999-2020": (standard(), "x")})
    assert found["doc"] and "такого стандарта нет" in found["note"]
    other = gp.find_text("ГОСТ 99999-2023", {"ГОСТ 99999-2020": (standard(), "x")})
    assert other["doc"] is None and "ГОСТ 99999-2020" in other["note"]


def test_shipped_profiles_quotes_are_in_standard_texts():
    """Каждый пункт поставляемых профилей data/gost_profiles стоит дословно в тексте стандарта корпуса."""
    texts = gp._texts()
    profiles = gp.all_profiles()
    if not profiles:
        pytest.skip("профили не собраны")
    for p in profiles:
        found = texts.get(p["designation"])
        if not found:
            continue
        doc = found[0]
        assert gp.text_sha(doc) == p["text_sha256"], p["designation"]
        segs = {s["clause"]: s["text"] for s in gp.segments(doc)}
        for it in p["items"]:
            assert it["quote"] in segs[it["clause"]], (p["designation"], it["id"])


def test_review_rejected_item_is_excluded(tmp_path, monkeypatch):
    built(tmp_path, monkeypatch)
    prof = gp.load("ГОСТ 99999-2020")
    lim = next(i for i in prof["items"] if i["kind"] == "limit" and i["clause"] == "2.1")
    (tmp_path / "_review.json").write_text(json.dumps({"items": [
        {"designation": "ГОСТ 99999-2020", "clause": "2.1", "quote": lim["quote"], "verdict": "rejected", "why": "тест"}]}, ensure_ascii=False), encoding="utf-8")
    doc = doc_of([("para", "ТЗ"), ("para", "Лифт по ГОСТ 99999-2020."), ("row", "Скорость, м/с | 1,6")])
    out = gp.apply(doc, [])
    assert not [f for f in out if f.rule_id == "gost_profile.limit"]
    assert all(c["sources"][0]["clause"] != "2.1" or c["kind"] != "limit" for c in gp.canonical(gp.all_profiles()))
