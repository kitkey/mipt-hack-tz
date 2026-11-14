import pytest

from tzcheck import pii
from tzcheck.pii import Scrubber

TEXT = ("Контактное лицо: Олег Ким, тел. 92-619 (доб. 5), e-mail: info@zavod.ru. "
        "Место поставки: г. Москва, ул. Ленина, д. 5. Подъёмник Alimak Scando 650 по ГОСТ 12.4.103-2020, "
        "ОКПД 2 28.22.14.150, КПП 783801001. Исполнитель ИП, ИНН 772112345678. Сайт https://zakupki.gov.ru")

STUB = [
    ("FIRST_NAME", "Олег", 0.99), ("LAST_NAME", "Ким", 0.98), ("PHONE", "92-619 (доб. 5)", 0.9),
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
    assert Scrubber().ner is False
    monkeypatch.setenv("TZCHECK_PII_NER", "1")
    assert Scrubber().ner is True


def test_ner_masks_people_and_contacts_only(stub_ner):
    scr = Scrubber(ner=True)
    out = scr.scrub(TEXT)
    assert "Олег Ким" not in out and "[PERSON_1]" in out
    assert "92-619" not in out and "[PHONE_1]" in out
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
    assert "Олег Ким" in out and "92-619" in out


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
        text = "звонить Ким Олегу тел. 8(4752) 71-26-94"
        spans = pii._predict_many([text])[0]
    finally:
        pii._engine = old
    merged = pii.person_merge(spans, text)
    assert [(k, text[s:e]) for k, s, e, _ in merged] == [("PERSON", "Ким Олегу"), ("PHONE", "8(4752) 71-26-94")]
