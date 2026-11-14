import json
import threading
import time

import numpy as np
import pytest

from tzcheck import gost_profiles as gp
from tzcheck.model import SECTION_UNCHECKED
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
        {"type": "text", "text": "2.3 Скорость в режиме ревизии должна быть не менее 0,3 м/с."},
        {"type": "text", "text": "3 Документация"},
        {"type": "text", "text": "3.1 Каждый лифт комплектуется паспортом и руководством по эксплуатации."},
        {"type": "text", "text": "Приложение А (обязательное) Форма паспорта"},
        {"type": "text", "text": "А.1 Паспорт содержит сведения о лифте."}]}


def model_items():
    return {"items": [
        {"kind": "order_param", "clause": "Введение", "quote": "условия окружающей среды (температура, влажность и т.п.)", "trigger": "заказчик и поставщик согласовывают",
         "param": "условия окружающей среды", "aliases": ["температура эксплуатации"], "requirement": "Согласовать условия окружающей среды.", "applies_to": "лифт"},
        {"kind": "limit", "clause": "2.1", "quote": "Номинальная скорость лифта должна быть не более 1,0 м/с", "param": "скорость", "aliases": ["скорость движения кабины"],
         "requirement": "Скорость не более 1 м/с.", "applies_to": "лифт", "limit": {"op": "max", "min": None, "max": 1.0, "unit": "м/с"}},
        {"kind": "limit", "clause": "2.2", "quote": "Время работы от резервного источника питания должно быть не менее 60 минут",
         "param": "время работы от резервного питания", "requirement": "Не менее 60 минут.", "applies_to": "лифт", "limit": {"op": "min", "min": "60", "unit": "мин"}},
        {"kind": "docs", "clause": "3.1", "quote": "Каждый лифт комплектуется паспортом", "param": "паспорт лифта", "aliases": ["паспорт"],
         "requirement": "Передать паспорт.", "applies_to": "лифт"},
        # модель перепутала оператор: в тексте «не менее 0,3 м/с»; сверка это ловит
        {"kind": "limit", "clause": "2.3", "quote": "Скорость в режиме ревизии должна быть не менее 0,3 м/с", "param": "скорость ревизии",
         "limit": {"op": "max", "max": 0.3, "unit": "м/с"}},
        # пример, а не требование
        {"kind": "docs", "clause": "А.1", "quote": "Паспорт содержит сведения о лифте", "param": "форма паспорта"},
        # форма ответа не та: в профиль не попадают, остаются черновиками
        {"kind": "limit", "clause": "9.9", "quote": "Номинальная скорость лифта", "param": "скорость", "limit": {"op": "max", "max": 1.0, "unit": "м/с"}},
        {"kind": "method", "clause": "2.1", "quote": "Номинальная скорость лифта", "param": "скорость"},
        {"kind": "limit", "clause": "2.1", "quote": "Номинальная скорость лифта", "param": "скорость", "limit": {"op": "около", "max": 1.0}},
        {"kind": "limit", "clause": "2.1", "quote": "Номинальная скорость лифта", "param": "скорость без числа", "limit": {"op": "eq", "unit": "м/с"}},
    ]}


class FakeModel:
    """Модель для тестов: отвечает на три вида запросов (извлечение, сверка, применимость) и записывает запросы."""

    def __init__(self, applies=("лифт",), slow=0.0):
        self.prompts = []
        self.applies = applies
        self.slow = slow
        self.lock = threading.Lock()

    def __call__(self, prompt):
        with self.lock:
            self.prompts.append(prompt)
        time.sleep(self.slow)
        if prompt.startswith("Позиция закупки"):
            pos = prompt.split("Ниже раздел")[0]
            ok = any(w in pos for w in self.applies)
            return {"applies": ok, "why": "лифт назван в области применения" if ok else "другая продукция", "quote": "распространяется на лифты"}
        if "Сверь каждый пункт" in prompt:
            block = prompt.split("Пункты чеклиста:")[1].split("<<<СТАНДАРТ>>>")[0]
            out = []
            for line in block.strip().splitlines():
                it = json.loads(line)
                lim = it.get("limit") or {}
                matches = not (lim.get("op") == "max" and "не менее" in it["quote"])
                nature = "пример" if it["param"] == "форма паспорта" else "требование"
                out.append({"id": it["id"], "matches": matches, "why": "оператор в тексте «не менее»" if not matches else "совпадает",
                            "nature": nature, "applies_to": {"product": "лифт", "variant": "", "conditions": ""},
                            "superseded": {"is": False, "by": "", "quote": ""}})
            return {"items": out}
        return model_items()

    def count(self, marker):
        return sum(1 for p in self.prompts if marker in p)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_GOST_PROFILES", str(tmp_path))
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)
    monkeypatch.setattr(gp, "_texts", lambda: {"ГОСТ 99999-2020": (standard(), "x")})
    monkeypatch.setattr(gp, "_embed", fake_embed)
    monkeypatch.setattr(gp, "PASSES", 1)
    gp.BUILD_ERRORS.clear()
    return tmp_path


def fake_embed(texts):
    """Детерминированные векторы: одинаковые параметры совпадают, разные ортогональны."""
    vocab = {}
    out = []
    for t in texts:
        key = t.split(":")[0].split("(")[0].strip().lower()
        k = vocab.setdefault(key, len(vocab))
        v = np.zeros(64)
        v[k % 64] = 1.0
        out.append(v)
    return np.array(out)


def test_segments_skip_preface_and_notes():
    segs = gp.segments(standard())
    clauses = [s["clause"] for s in segs]
    assert clauses == ["Введение", "1", "2", "2.1", "2.2", "2.3", "3", "3.1", "Приложение А", "А.1"]
    assert "Примечание к пункту" in next(s for s in segs if s["clause"] == "2.1")["text"]
    assert next(s for s in segs if s["clause"] == "2.2")["tables"]


def test_figure_legend_is_not_a_clause():
    doc = {"blocks": [{"type": "text", "text": "1 Область применения"}, {"type": "text", "text": "2 Требования"},
                      {"type": "text", "text": "2.1 Пост управления"}, {"type": "text", "text": "3 - переключатель режима"},
                      {"type": "text", "text": "2.2 Следующий пункт"}]}
    assert [s["clause"] for s in gp.segments(doc)] == ["1", "2", "2.1", "2.2"]


def test_item_form_only_structural_checks():
    clauses = gp.clause_set(gp.segments(standard()))
    kept, reasons = [], []
    for raw in model_items()["items"]:
        it, why = gp._item(raw, clauses)
        (kept.append(it) if it else reasons.append(why))
    assert len(kept) == 6
    assert kept[2]["limit"] == {"op": "min", "min": 60.0, "max": None, "values": [], "unit": "мин"}
    assert any("9.9" in r for r in reasons) and any("method" in r for r in reasons) and any("около" in r for r in reasons)
    assert any("нет значения" in r for r in reasons)
    assert gp._value({"op": "min", "min": None, "max": None, "values": ["IP 2XC"]}) == "IP 2XC"  # не число: строкой, как у модели
    assert gp._value({"op": "range", "min": 1.0, "max": 2.0, "values": []}) == [1.0, 2.0]


def test_module_has_no_number_or_unit_parsing():
    # всё содержательное сверяет модель: регулярок для чисел, единиц и операторов в модуле нет
    for name in ("NUMBER_RE", "UNIT_RE", "OP_WORDS", "QUALIFIER_RE", "CUSTOMER_RE", "parse_number", "numbers_in", "norm_unit",
                 "locate_quote", "validate_item", "_op_conflict"):
        assert not hasattr(gp, name), name


def test_prompts_are_not_tied_to_one_product():
    text = (gp.EXTRACT_PROMPT + gp.VERIFY_PROMPT + gp.APPLIES_PROMPT + gp.SAME_PROMPT + gp.SYSTEM).lower()
    for w in ("лифт", "кабин", "шахт", "приямк", "ловител", "буфер"):
        assert w not in text, w


def test_build_without_model_returns_none(env):
    assert gp.build("ГОСТ 99999-2020") is None


def test_build_two_passes_statuses_and_cache(env):
    m = FakeModel()
    prof = gp.build("ГОСТ 99999-2020", complete=m)
    by = {i["param"]: i for i in prof["items"]}
    assert set(by) == {"условия окружающей среды", "скорость", "время работы от резервного питания", "паспорт лифта"}
    assert all(i["status"] == gp.STATUS_VERIFIED and i["verification"]["matches"] for i in prof["items"])
    drafts = {d.get("param") or d["raw"].get("param"): d["reason"] for d in prof["drafts"]}
    assert "оператор" in drafts["скорость ревизии"] and "пример" in drafts["форма паспорта"]
    assert prof["counts"]["by_status"] == {"draft": 6, "verified": 4, "approved": 0}
    # сверке дают полный текст пункта, область применения и соседние пункты
    ver = next(p for p in m.prompts if "Сверь каждый пункт" in p and "скорость ревизии" in p)
    assert "[2.3] 2.3 Скорость в режиме ревизии" in ver and "распространяется на лифты" in ver and "[2.2]" in ver and "[3]" in ver
    assert "[2] 2 Требования" in ver  # заголовок раздела над пунктом
    assert gp._parents("5.9.3.8.1") == ["5", "5.9", "5.9.3", "5.9.3.8"] and gp._parents("А.2.1") == ["Приложение А", "А.2"]
    again = gp.build("ГОСТ 99999-2020", complete=lambda p: pytest.fail("модель не должна вызываться"))
    assert again["built_at"] == prof["built_at"]


def test_model_calls_go_through_llm_call(monkeypatch):
    from tzcheck import llm
    seen = {}
    monkeypatch.setattr(llm, "_call", lambda prompt, max_tokens=4000, doc_flags=None: seen.update(prompt=prompt, flags=doc_flags) or {"items": []})
    assert gp._complete("ЗАПРОС") == {"items": []}
    # сборка профиля: только текст стандарта, запрос помечен открытым
    assert seen["prompt"].startswith(gp.SYSTEM) and seen["prompt"].endswith("ЗАПРОС") and seen["flags"] == {"public": True}
    # применимость к позиции ТЗ: флаги документа из llm.doc_scope, не открытые по умолчанию
    gp._complete_tz("ПОЗИЦИЯ")
    assert seen["flags"] is None and seen["prompt"].endswith("ПОЗИЦИЯ")


def test_fatal_error_stops_requests_and_profile_is_not_cached(env, monkeypatch):
    class AuthenticationError(Exception):
        pass

    calls = []

    def bad(prompt):
        calls.append(1)
        raise AuthenticationError("ключ неверен")

    monkeypatch.setattr(gp, "windows", lambda segs, size=80: [f"окно {i}" for i in range(40)])
    assert gp.build("ГОСТ 99999-2020", complete=bad) is None
    assert len(calls) < 40 and "AuthenticationError" in gp.BUILD_ERRORS["ГОСТ 99999-2020"][0]
    assert not (env / "gost-99999-2020.json").exists()


def test_empty_answers_are_not_cached(env):
    assert gp.build("ГОСТ 99999-2020", complete=lambda p: {"items": []}) is None
    assert "ни одного пункта" in gp.BUILD_ERRORS["ГОСТ 99999-2020"][0] and not list(env.glob("gost-*.json"))


def test_deadline_passed_means_no_requests(env):
    m = FakeModel()
    assert gp.build("ГОСТ 99999-2020", complete=m, deadline=time.time() - 1) is None
    assert not m.prompts and "бюджет" in gp.BUILD_ERRORS["ГОСТ 99999-2020"][0]


def test_parallel_builds_of_one_profile_run_once(env):
    m = FakeModel(slow=0.05)
    res = []
    threads = [threading.Thread(target=lambda: res.append(gp.build("ГОСТ 99999-2020", complete=m, merge_after=False))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(res) == 3 and len({r["built_at"] for r in res}) == 1
    assert m.count("Выпиши из фрагмента") == len(gp.windows(gp.segments(standard())))
    assert not list(env.glob(".*.tmp")) and not list(env.glob(".*.lock"))


def test_atomic_write_uses_unique_temp_files(env, monkeypatch):
    names = []
    real = gp.tempfile.mkstemp

    def spy(**kw):
        fd, p = real(**kw)
        names.append(p)
        return fd, p

    monkeypatch.setattr(gp.tempfile, "mkstemp", spy)
    gp.save({"slug": "a", "items": []})
    gp.save({"slug": "a", "items": [1]})
    assert len(set(names)) == 2 and json.loads((env / "a.json").read_text(encoding="utf-8"))["items"] == [1]


def item(i, kind, param, req, lim=None, status="verified"):
    return {"id": f"x#{i}", "kind": kind, "param": param, "requirement": req, "quote": req, "clause": str(i), "aliases": [], "limit": lim,
            "status": status}


def test_merge_thresholds_model_and_limits(env):
    a = {"designation": "ГОСТ 1-2020", "slug": "a", "text_sha256": "1", "items": [
        item(1, "docs", "паспорт", "Передать паспорт."),
        item(2, "limit", "скорость", "Скорость не более 1 м/с.", {"op": "max", "min": None, "max": 1.0, "unit": "м/с", "values": []}),
        item(6, "docs", "формуляр", "Передать формуляр.", status="draft")]}
    b = {"designation": "ГОСТ 2-2021", "slug": "b", "text_sha256": "2", "items": [
        item(3, "docs", "паспорт", "Комплектуется паспортом."),
        item(4, "limit", "скорость", "Скорость не более 1,6 м/с.", {"op": "max", "min": None, "max": 1.6, "unit": "м/с", "values": []})]}
    out = gp.merge([a, b], judge=lambda p: {"decisions": []}, embed=fake_embed)
    by_param = {}
    for c in out["items"]:
        by_param.setdefault(c["param"], []).append(c)
    assert len(by_param["паспорт"]) == 1 and len(by_param["паспорт"][0]["sources"]) == 2
    assert len(by_param["скорость"]) == 2 and "формуляр" not in by_param
    assert any(d["decision"] == "не слито" and "разные" in d["by"] for d in out["decisions"])


def test_merge_gray_zone_goes_to_model_and_answer_is_reused(env):
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
    assert out["canonical_total"] == 2 and out["decisions"][0]["by"] == "модель"
    out = gp.merge([p], judge=None, embed=embed)
    assert out["decisions"][0]["why"] == "разные документы"
    (env / "_canonical.json").unlink()
    out = gp.merge([p], judge=None, embed=embed)
    assert "модель выключена" in out["decisions"][0]["why"]


def test_rule_topics_mark_covered(env):
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


def test_approve_items_by_human(env):
    prof = gp.build("ГОСТ 99999-2020", complete=FakeModel())
    sl = prof["slug"]
    lim = next(i for i in prof["items"] if i["param"] == "скорость")
    draft = prof["drafts"][0]["id"]
    res = gp.approve(sl, [lim["id"], draft, "нет#1"], "Иванов")
    assert res["approved"] == [lim["id"]] and {s["id"] for s in res["skipped"]} == {draft, "нет#1"}
    saved = gp.load_slug(sl)
    it = next(i for i in saved["items"] if i["id"] == lim["id"])
    assert it["status"] == "approved" and it["approved_by"] == "Иванов" and it["approved_at"]
    assert saved["counts"]["by_status"]["approved"] == 1
    statuses = {r["text"]: r["status"] for r in gp.lexicon_records()}
    assert statuses["скорость"] == "active" and statuses["паспорт лифта"] == "candidate"
    # утверждение переживает пересборку того же текста
    again = gp.build("ГОСТ 99999-2020", complete=FakeModel(), force=True)
    assert next(i for i in again["items"] if i["param"] == "скорость")["status"] == "approved"
    gp.approve(sl, [lim["id"]], "Иванов", revoke=True)
    assert next(i for i in gp.load_slug(sl)["items"] if i["id"] == lim["id"])["status"] == "verified"
    with pytest.raises(ValueError):
        gp.approve(sl, [lim["id"]], " ")
    with pytest.raises(KeyError):
        gp.approve("нет-такого", [lim["id"]], "Иванов")


def test_items_for_position_uses_model_applicability_with_cache(env):
    gp.build("ГОСТ 99999-2020", complete=FakeModel())
    m = FakeModel()
    pos = {"name": "Работы по замене грузового лифта", "kind": "лифт грузовой", "okpd2": "43.29.19.120"}
    items = gp.items_for(pos, {"gosts": ["ГОСТ 99999-2020"]}, complete=m)
    assert {i["param"] for i in items} == {"условия окружающей среды", "скорость", "время работы от резервного питания", "паспорт лифта"}
    speed = next(i for i in items if i["param"] == "скорость")
    assert speed["slug"] == "gost-99999-2020" and speed["designation"] == "ГОСТ 99999-2020" and speed["clause"] == "2.1"
    assert speed["clause_text"].startswith("2.1 Номинальная скорость") and speed["operator"] == "не более" and speed["value"] == 1.0
    assert speed["unit"] == "м/с" and speed["status"] == "verified" and speed["applies_to"]["product"] == "лифт"
    assert speed["sources"][0]["applicability"]["why"]
    assert m.count("Позиция закупки") == 1
    gp.items_for(pos, {"gosts": ["ГОСТ 99999-2020"]}, complete=m)
    assert m.count("Позиция закупки") == 1  # кэш по (slug, вид изделия)
    assert gp.items_for({"name": "Поставка бумаги офисной"}, {"gosts": ["ГОСТ 99999-2020"]}, complete=m) == []
    assert gp.items_for(pos, {"gosts": []}, complete=m) == []
    # без модели незакэшированный вид изделия не решён: пунктов нет
    assert gp.items_for({"name": "Насос"}, {"gosts": ["ГОСТ 99999-2020"]}) == []


def test_api_lists_and_approves(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    prof = gp.build("ГОСТ 99999-2020", complete=FakeModel())
    app = FastAPI()
    app.include_router(gp.router)
    c = TestClient(app)
    rows = c.get("/api/gost_profiles").json()
    assert rows[0]["slug"] == prof["slug"] and rows[0]["by_status"]["verified"] == 4
    one = prof["items"][0]["id"]
    r = c.post(f"/api/gost_profiles/{prof['slug']}/approve", json={"item_ids": [one], "user": "Петров"})
    assert r.status_code == 200 and r.json()["approved"] == [one]
    assert c.get("/api/gost_profiles").json()[0]["by_status"]["approved"] == 1
    one_view = c.get(f"/api/gost_profiles/{prof['slug']}").json()
    assert one_view["designation"] == "ГОСТ 99999-2020" and one_view["by_status"]["approved"] == 1
    row = next(i for i in one_view["items"] if i["id"] == one)
    assert row["status"] == "approved" and row["status_title"] == gp.STATUS_TITLES["approved"] and row["approved_by"] == "Петров"
    assert row["kind_title"] and row["quote"] and row["clause"] and row["nature"] == "требование" and row["verify_reason"] == "совпадает"
    speed = next(i for i in one_view["items"] if i["param"] == "скорость")
    assert speed["limit"]["op"] == "max" and speed["limit"]["max"] == 1.0 and speed["can_approve"]
    drafts = {d["param"]: d for d in one_view["drafts"]}
    assert len(one_view["drafts"]) == 6 and not any(d["can_approve"] for d in one_view["drafts"])
    assert drafts["скорость ревизии"]["verify_reason"] and drafts["скорость ревизии"]["status"] == "draft"
    assert drafts["скорость"]["clause"] in ("9.9", "2.1") and drafts["скорость"]["draft_reason"]  # черновик первого прохода: поля из raw
    assert c.get("/api/gost_profiles/nope").status_code == 404
    assert c.post("/api/gost_profiles/nope/approve", json={"item_ids": [one], "user": "Петров"}).status_code == 404
    assert c.post(f"/api/gost_profiles/{prof['slug']}/approve", json={"item_ids": [one], "user": ""}).status_code == 400


def doc_of(rows):
    return ParsedDoc("tz.docx", [Block(f"p{i}", "para", t, "Раздел") for i, t in enumerate(rows)], {}, [], "docx")


def test_apply_marks_unavailable_and_not_built(env):
    doc = doc_of(["ТЗ", "Лифт по ГОСТ 99999-2020 и ГОСТ 12345-2019."])
    out = gp.apply(doc, [], build_missing=True)
    by = {f.rule_id: f for f in out}
    assert by["gost_profile.unavailable"].section == SECTION_UNCHECKED and "ГОСТ 12345-2019" in by["gost_profile.unavailable"].explanation
    assert "модель выключена" in by["gost_profile.not_built"].explanation
    gp.build("ГОСТ 99999-2020", complete=FakeModel())
    assert "gost_profile.not_built" not in {f.rule_id for f in gp.apply(doc, [])}


def test_r_prefix_error_uses_existing_text(monkeypatch):
    monkeypatch.setattr(gp, "_exists", lambda d: False)
    found = gp.find_text("ГОСТ Р 99999-2020", {"ГОСТ 99999-2020": (standard(), "x")})
    assert found["doc"] and "такого стандарта нет" in found["note"]
    other = gp.find_text("ГОСТ 99999-2023", {"ГОСТ 99999-2020": (standard(), "x")})
    assert other["doc"] is None and "ГОСТ 99999-2020" in other["note"]


def test_shipped_profiles_are_consistent():
    """Поставляемые профили data/gost_profiles: в чеклисте только verified и approved, пункт есть в нумерации текста."""
    profiles = gp.all_profiles()
    if not profiles:
        pytest.skip("профили не собраны")
    texts = gp._texts()
    for p in profiles:
        assert p["prompt_version"] == gp.PROMPT_VERSION
        found = texts.get(p["designation"])
        clauses = gp.clause_set(gp.segments(found[0])) if found else None
        for it in p["items"]:
            assert it["status"] in gp.CHECKLIST, (p["designation"], it["id"])
            if clauses is not None:
                assert it["clause"] in clauses, (p["designation"], it["clause"])


def test_profiles_live_in_refdata_volume_seeded_from_bundle(tmp_path, monkeypatch):
    """В Docker профили лежат в томе справочников (TZCHECK_DATA_DIR=/refdata): поставка докладывается туда, утверждения пишутся туда."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "gost-1-2020.json").write_text(json.dumps({"slug": "gost-1-2020", "designation": "ГОСТ 1-2020", "items": [
        {"id": "gost-1-2020#1", "kind": "docs", "param": "паспорт", "status": "verified"}], "drafts": []}), encoding="utf-8")
    vol = tmp_path / "refdata"
    monkeypatch.delenv("TZCHECK_GOST_PROFILES", raising=False)
    monkeypatch.setenv("TZCHECK_DATA_DIR", str(vol))
    monkeypatch.setattr(gp, "BUNDLED", bundle)
    monkeypatch.setattr(gp, "_seeded", set())
    assert gp.profile_dir() == vol / "gost_profiles" and (vol / "gost_profiles" / "gost-1-2020.json").exists()
    gp.approve("gost-1-2020", ["gost-1-2020#1"], "Иванов")
    assert json.loads((bundle / "gost-1-2020.json").read_text(encoding="utf-8"))["items"][0]["status"] == "verified"
    monkeypatch.setattr(gp, "_seeded", set())  # перезапуск контейнера: утверждение в томе не затирается поставкой
    assert gp.load_slug("gost-1-2020")["items"][0]["status"] == "approved"


def test_shipped_limits_have_values():
    for p in gp.all_profiles():
        for it in p["items"]:
            if it["kind"] == "limit":
                assert gp._value(it["limit"]) not in (None, []), (p["designation"], it["id"])
