import pytest

from tzcheck import llm, pii
from tzcheck.parse import ParsedDoc
from tzcheck.pii import Scrubber

TEXT = ("Контактное лицо: Олег Ким, тел. 45-123 (доб. 5), e-mail: info@zavod.ru. "
        "Место поставки: г. Москва, ул. Ленина, д. 5. Подъёмник Alimak Scando 650 по ГОСТ 12.4.103-2020, "
        "ОКПД 2 28.22.14.150, КПП 783801001. Исполнитель ИП, ИНН 772112345678. Сайт https://zakupki.gov.ru")

STUB = [
    ("FIRST_NAME", "Олег", 0.99), ("LAST_NAME", "Ким", 0.98), ("PHONE", "45-123 (доб. 5)", 0.9),
    ("CITY", "Москва", 0.99), ("STREET", "Ленина", 0.99), ("HOUSE", "5", 0.9),
    ("INN", "783801001", 0.9), ("INN", "772112345678", 0.95), ("URL", "https://zakupki.gov.ru", 0.99),
    ("LAST_NAME", "Scando", 0.2), ("PASSPORT", "12.4.103-2020", 0.8),
]


def fake_predict(texts):
    return [fake_one(t) for t in texts]


def fake_one(text):
    out = []
    for kind, value, score in STUB:
        i = text.find(value)
        if i >= 0:
            out.append((kind, i, i + len(value), score))
    return out


@pytest.fixture
def stub_ner(monkeypatch):
    monkeypatch.setattr(pii, "_load", lambda: object())
    monkeypatch.setattr(pii, "_predict_many", fake_predict)
    monkeypatch.setattr(pii, "_cache", {})
    monkeypatch.delenv("TZCHECK_PII_NER_MIN", raising=False)


def test_ner_off_by_default(monkeypatch):
    monkeypatch.delenv("TZCHECK_PII_NER", raising=False)
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)
    assert pii.ner_mode() == "auto" and Scrubber().ner is False
    monkeypatch.setenv("TZCHECK_PII_NER", "1")
    assert Scrubber().ner is True


def _model(monkeypatch, model, base=None):
    monkeypatch.setenv("TZCHECK_LLM_MODEL", model)
    if base is None:
        monkeypatch.delenv("TZCHECK_LLM_API_BASE", raising=False)
    else:
        monkeypatch.setenv("TZCHECK_LLM_API_BASE", base)


@pytest.mark.parametrize("model,base,external", [
    ("openai/qwen", "http://localhost:8091/v1", False),
    ("openai/qwen", "http://127.0.0.1:8091/v1", False),
    ("openai/qwen", "http://host.docker.internal:8091/v1", False),
    ("ollama/qwen", None, False),
    ("openrouter/qwen/qwen3", None, True),
    ("openrouter/qwen/qwen3", "http://localhost:8091/v1", True),
    ("openai/qwen", "https://llm.example.com/v1", True),
    ("gpt-4o", None, True),
])
def test_auto_mode_follows_model_address(monkeypatch, model, base, external):
    monkeypatch.delenv("TZCHECK_PII_NER", raising=False)
    _model(monkeypatch, model, base)
    assert pii.external_model() is external
    assert Scrubber().ner is external
    monkeypatch.setenv("TZCHECK_PII_NER", "0")
    assert Scrubber().ner is False
    monkeypatch.setenv("TZCHECK_PII_NER", "1")
    assert Scrubber().ner is True


def _doc(public=False):
    d = ParsedDoc("t.docx", [], {}, [], "docx")
    if public:
        d.public = True
    return d


@pytest.fixture
def external_no_ner(monkeypatch):
    for k in ("TZCHECK_PII_NER", "TZCHECK_DOC_PUBLIC", "TZCHECK_EXTERNAL_MASKED"):
        monkeypatch.delenv(k, raising=False)
    _model(monkeypatch, "openrouter/qwen/qwen3")
    monkeypatch.setattr(pii, "_load", lambda: None)
    sent = []
    monkeypatch.setitem(__import__("sys").modules, "litellm", type("L", (), {"completion": staticmethod(lambda **kw: sent.append(kw))}))
    return sent


def test_allow_external(external_no_ner, monkeypatch):
    assert llm.allow_external() is False
    assert llm.allow_external({"public": True}) is True
    monkeypatch.setenv("TZCHECK_DOC_PUBLIC", "1")
    assert llm.allow_external() is True
    monkeypatch.delenv("TZCHECK_DOC_PUBLIC")
    monkeypatch.setattr(pii, "_load", lambda: object())
    # закрытый документ при доступном NER без разрешения заказчика наружу не уходит
    assert llm.allow_external() is False
    monkeypatch.setenv("TZCHECK_EXTERNAL_MASKED", "1")
    assert llm.allow_external() is True
    monkeypatch.setenv("TZCHECK_PII_NER", "0")
    assert llm.allow_external() is False
    _model(monkeypatch, "openai/qwen", "http://localhost:8091/v1")
    assert llm.allow_external() is True


def test_closed_doc_with_ner_needs_customer_permission(external_no_ner, monkeypatch):
    monkeypatch.setattr(pii, "_load", lambda: object())
    doc = _doc()
    assert llm.gate(doc) is False and doc.warnings == [llm.BLOCKED]
    with pytest.raises(llm.ExternalBlocked):
        llm._call("ТЗ")
    assert external_no_ner == []
    monkeypatch.setenv("TZCHECK_EXTERNAL_MASKED", "1")
    assert llm.gate(_doc()) is True
    monkeypatch.setenv("TZCHECK_EXTERNAL_MASKED", "0")
    assert llm.gate(_doc(public=True)) is True and llm.allow_external({"public": True}) is True


def test_call_blocked_before_sending(external_no_ner):
    with pytest.raises(llm.ExternalBlocked):
        llm._call("ТЗ")
    doc = _doc()
    assert llm._safe(lambda p: llm._call(p), "ТЗ", doc) == []
    assert doc.warnings == [llm.BLOCKED]
    assert external_no_ner == []
    with llm.doc_scope(_doc(public=True)):
        with pytest.raises(Exception):
            llm._call("ТЗ")  # пропущен к модели: заглушка litellm вернула None
    assert len(external_no_ner) == 1


def test_gate_warns_and_falls_back_to_rules(external_no_ner):
    doc = _doc()
    assert llm.gate(doc) is False
    assert doc.warnings == [llm.NER_DOWN, llm.BLOCKED]
    public = _doc(public=True)
    assert llm.gate(public) is True and public.warnings == [llm.NER_DOWN]


@pytest.fixture
def local_main_external_stage(monkeypatch):
    """Общая модель локальная, у второй модели второй проверки внешний поставщик; NER не загружается, документ не открытый."""
    for k in ("TZCHECK_PII_NER", "TZCHECK_DOC_PUBLIC", "TZCHECK_VERIFY2_API_BASE", "TZCHECK_VERIFY2_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    _model(monkeypatch, "openai/qwen", "http://localhost:8091/v1")
    monkeypatch.setenv("TZCHECK_VERIFY2_MODEL", "openrouter/other/model")
    monkeypatch.setattr(pii, "_load", lambda: None)
    sent = []

    def completion(**kw):
        sent.append(kw)
        raise RuntimeError("заглушка")
    monkeypatch.setitem(__import__("sys").modules, "litellm", type("L", (), {"completion": staticmethod(completion)}))
    return sent


def test_stage_model_needs_admission_by_its_own_address(local_main_external_stage, monkeypatch):
    sent = local_main_external_stage
    assert llm.allow_external() is True and llm.allow_external(stage="verify2") is False
    assert llm.allow_external({"public": True}, stage="verify2") is True
    with pytest.raises(llm.ExternalBlocked):
        llm._call("ТЗ", stage="verify2")
    assert sent == []
    with pytest.raises(RuntimeError):
        llm._call("ТЗ")
    assert [kw["model"] for kw in sent] == ["openai/qwen"]
    monkeypatch.setenv("TZCHECK_VERIFY2_API_BASE", "http://127.0.0.1:9000/v1")
    monkeypatch.setenv("TZCHECK_VERIFY2_MODEL", "openai/second")
    assert llm.allow_external(stage="verify2") is True
    monkeypatch.setenv("TZCHECK_VERIFY2_MODEL", "openrouter/other/model")
    assert llm.allow_external(stage="verify2") is False  # openrouter/ внешний при любом адресе, как у общей модели
    monkeypatch.delenv("TZCHECK_VERIFY2_API_BASE")
    monkeypatch.setenv("TZCHECK_VERIFY2_MODEL", "gpt-4o")
    assert llm.allow_external(stage="verify2") is False  # без своего адреса модель шага идёт в облако поставщика
    monkeypatch.setenv("TZCHECK_DOC_PUBLIC", "1")
    assert llm.allow_external(stage="verify2") is True
    with pytest.raises(RuntimeError):
        llm._call("ТЗ", stage="verify2")
    assert [kw["model"] for kw in sent] == ["openai/qwen", "gpt-4o"]


def test_pipeline_external_stage_model_not_called_for_closed_doc(local_main_external_stage, monkeypatch, scripted):
    import sys
    from conftest import fake_litellm
    from tzcheck.pipeline import check
    for k in ("TZCHECK_DOC_PUBLIC", "TZCHECK_ORCHESTRATOR", "TZCHECK_RAG", "TZCHECK_CUSTOMER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    _model(monkeypatch, "openai/qwen", "http://localhost:8091/v1")
    models = []
    inner = fake_litellm(scripted).completion

    def completion(**kw):
        models.append(kw["model"])
        return inner(**kw)
    monkeypatch.setitem(sys.modules, "litellm", type("L", (), {"completion": staticmethod(completion)}))
    r = check("samples/lift_fin.docx", use_llm=True)
    assert models and set(models) == {"openai/qwen"}
    assert any("ExternalBlocked" in w for w in r["warnings"])
    assert r["status"] == "incomplete" and "core_verify" in r["completeness"]["incomplete_steps"]
    models.clear()
    r = check("samples/lift_fin.docx", use_llm=True, public=True)
    assert "openrouter/other/model" in models and "core_verify" not in r["completeness"]["incomplete_steps"]


@pytest.mark.parametrize("orchestrator", ["linear", "graph"])
def test_pipeline_external_without_ner_skips_model(external_no_ner, monkeypatch, orchestrator):
    from tzcheck.pipeline import check
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    monkeypatch.setattr(llm, "_call", lambda *a, **k: pytest.fail("модель вызвана"))
    res = check("samples/lift_fin.docx", orchestrator=orchestrator)
    assert res["mode"] == "format" and res["summary"].startswith("Модель не вызывалась")
    assert llm.BLOCKED in res["warnings"] and llm.NER_DOWN in res["warnings"]
    assert external_no_ner == []


@pytest.mark.parametrize("text,masked,kept", [
    ("Паспорт серия 45 11 № 123456 выдан ОВД", "45 11 № 123456", "Паспорт серия "),
    ("паспорт: 4511 123456", "4511 123456", "паспорт: "),
    ("Заказчик ИНН 7701234567, КПП 770101001", None, "7701234567"),
    ("Извещение № 3269990001 в ЕИС", None, "3269990001"),
    ("Счёт для возврата 40817810099910004312", "40817810099910004312", None),
    ("р/с 40702810900000001234 в банке", None, "40702810900000001234"),
])
def test_regex_passport_needs_context_and_personal_account(text, masked, kept):
    scr = Scrubber(ner=False)
    out = scr.scrub(text)
    if masked:
        assert masked not in out
    if kept:
        assert kept in out
    assert scr.restore(out) == text


def test_ner_masks_people_and_contacts_only(stub_ner):
    scr = Scrubber(ner=True)
    out = scr.scrub(TEXT)
    assert "Олег Ким" not in out and "[PERSON_1]" in out
    assert "45-123" not in out and "[PHONE_1]" in out
    assert "772112345678" not in out and "[INN_1]" in out
    for keep in ("г. Москва, ул. Ленина, д. 5", "Alimak Scando 650", "ГОСТ 12.4.103-2020", "28.22.14.150",
                 "КПП 783801001", "https://zakupki.gov.ru"):
        assert keep in out
    assert scr.summary() == {"EMAIL": 1, "PERSON": 1, "PHONE": 1, "INN": 1}
    assert scr.restore(out) == TEXT


def test_ner_skips_regex_tokens_and_reuses_tokens(stub_ner, monkeypatch):
    monkeypatch.setattr(pii, "_predict_many", lambda ts: [[("EMAIL", t.find("info@"), t.find("info@") + 13, 0.99)] for t in ts])
    scr = Scrubber(ner=True)
    out = scr.scrub("почта info@zavod.ru")
    assert out == "почта [EMAIL_1]" and scr.summary() == {"EMAIL": 1}
    scr2 = Scrubber(ner=True)
    monkeypatch.setattr(pii, "_predict_many", fake_predict)
    a, b = scr2.scrub("звонить Олег Ким"), scr2.scrub("ответственный Олег Ким")
    assert a.endswith("[PERSON_1]") and b.endswith("[PERSON_1]")


def test_prefetch_fills_cache_once(stub_ner, monkeypatch):
    calls = []
    monkeypatch.setattr(pii, "_predict_many", lambda ts: calls.append(list(ts)) or fake_predict(ts))
    scr = Scrubber(ner=True)
    scr.prefetch([TEXT, TEXT, "", "Олег Ким"])
    assert calls == [[TEXT, "Олег Ким"]]
    out = scr.scrub(TEXT)
    assert len(calls) == 1 and "[PERSON_1]" in out and scr.restore(out) == TEXT
    Scrubber(ner=False).prefetch(["другой текст"])
    assert len(calls) == 1


def test_ner_threshold(stub_ner, monkeypatch):
    monkeypatch.setenv("TZCHECK_PII_NER_MIN", "0.995")
    out = Scrubber(ner=True).scrub(TEXT)
    assert "Олег Ким" in out and "45-123" in out


def test_ner_unavailable_falls_back_to_regex(monkeypatch):
    monkeypatch.setattr(pii, "_load", lambda: None)
    monkeypatch.setattr(pii, "_cache", {})
    plain, layered = Scrubber(ner=False), Scrubber(ner=True)
    assert layered.scrub(TEXT) == plain.scrub(TEXT)
    assert layered.summary() == plain.summary() == {"EMAIL": 1}


def test_regex_layer_unchanged():
    scr = Scrubber(ner=False)
    out = scr.scrub("Иванов И.И., тел. +7 (495) 123-45-67, ivanov@mail.ru, СНИЛС 123-456-789 01")
    assert out == "[PERSON_1], тел. [PHONE_1], [EMAIL_1], СНИЛС [SNILS_1]"


def test_predict_merges_windows_and_fragments():
    torch = pytest.importorskip("torch")

    class Tok:
        def __call__(self, texts, **kw):
            text = texts[0]
            words = [(i, i + len(w)) for i, w in ((m.start(), m.group()) for m in __import__("re").finditer(r"\S+", text))]
            return {"input_ids": torch.zeros(1, len(words) + 2, dtype=torch.long),
                    "attention_mask": torch.ones(1, len(words) + 2, dtype=torch.long),
                    "offset_mapping": torch.tensor([[(0, 0)] + words + [(0, 0)]])}

    labels = ["O", "B-LAST_NAME", "B-FIRST_NAME", "O", "B-PHONE", "I-PHONE"]

    class Model:
        config = type("C", (), {"id2label": dict(enumerate(labels))})

        def __call__(self, input_ids, attention_mask):
            seq = [0, 0, 1, 2, 3, 4, 5, 0]
            logits = torch.full((1, len(seq), len(labels)), -5.0)
            for i, lab in enumerate(seq):
                logits[0, i, lab] = 5.0
            return type("R", (), {"logits": logits})

    old = pii._engine
    pii._engine = (torch, Tok(), Model(), "cpu")
    try:
        text = "звонить Ким Олегу тел. 8(4999) 11-22-33"
        spans = pii._predict_many([text])[0]
    finally:
        pii._engine = old
    merged = pii.person_merge(spans, text)
    assert [(k, text[s:e]) for k, s, e, _ in merged] == [("PERSON", "Ким Олегу"), ("PHONE", "8(4999) 11-22-33")]
