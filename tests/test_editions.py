"""Сверка ТЗ с новой редакцией стандарта и получение текстов редакций: локальные данные, без сети."""
import gzip
import json
import os

import pytest

import tzcheck.rag as rag
from tzcheck import refdata
from tzcheck.model import Finding
from tzcheck.parse import Block, ParsedDoc
from tzcheck.rag import editions
from tzcheck.updater import gost_texts
from tzcheck.updater.base import Context

OLD, NEW = "ГОСТ 3999-2010", "ГОСТ 3999-2020"
TITLE = "Лифты. Кнопки вызова и сигнализация"


def edition(designation, buttons, signal, extra=None):
    blocks = [
        {"type": "text", "text": f"МЕЖГОСУДАРСТВЕННЫЙ СТАНДАРТ {designation}"},
        {"type": "text", "text": TITLE},
        {"type": "text", "text": "1 Область применения"},
        {"type": "text", "text": "Настоящий стандарт распространяется на пассажирские лифты и устанавливает требования к устройствам вызова."},
        {"type": "text", "text": "4 Требования к устройствам вызова и сигнализации"},
        {"type": "text", "text": f"4.1 Кнопки вызова на этажах должны располагаться на высоте {buttons} от уровня пола этажной площадки."},
        {"type": "text", "text": "4.2 Указатель положения кабины должен быть установлен над дверями шахты на основном посадочном этаже."},
        {"type": "text", "text": f"4.3 Звуковой сигнал прибытия кабины {signal}."},
    ]
    if extra:
        blocks.append({"type": "text", "text": extra})
    return {"id": gost_texts.doc_id(designation), "document": designation, "edition": designation[-4:], "edition_date": None,
            "status": "Действует", "source": "https://example.org/" + designation[-4:], "retrieved": "2026-11-14", "blocks": blocks}


def status(designation):
    key = refdata.norm_designation(designation)
    if key == OLD:
        return {"designation": OLD, "status": "Заменён", "replaced_by": NEW, "inactive": True, "title": TITLE, "date_end": "01.01.2021"}
    if key == NEW:
        return {"designation": NEW, "status": "Действует", "inactive": False, "title": TITLE}
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    corpus, data = tmp_path / "corpus", tmp_path / "data"
    corpus.mkdir()
    data.mkdir()
    monkeypatch.setenv("TZCHECK_DATA_DIR", str(data))
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    monkeypatch.setenv("TZCHECK_EDITIONS_DENSE", "0")
    monkeypatch.setattr(editions, "CORPUS", corpus)
    monkeypatch.setattr(rag, "CORPUS", corpus)
    monkeypatch.setattr(rag, "MAIN_CORPUS", corpus)
    monkeypatch.setattr(refdata, "gost_status", status)
    editions._ENC.clear()
    return corpus, data


def save_text(doc):
    path = gost_texts.doc_path(doc["document"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)


def both_editions(corpus):
    (corpus / "old.json").write_text(json.dumps(edition(OLD, "от 900 до 1100 мм", "должен иметь уровень не менее 35 дБА"),
                                                ensure_ascii=False), encoding="utf8")
    save_text(dict(edition(NEW, "от 850 до 1100 мм", "может иметь уровень не менее 35 дБА",
                           "4.4 Кнопки вызова оснащаются рельефной маркировкой для незрячих пассажиров."), replaces=OLD))


def tz(*rows):
    blocks = [Block("p1", "paragraph", f"Лифт должен соответствовать {OLD}.")]
    for n, text in enumerate(rows, 1):
        blocks.append(Block(f"t1r{n}", "table_row", text, section="Технические характеристики", table=1, row=n, cells=text.split(" | ")))
    return ParsedDoc("tz.docx", blocks, {}, [], "docx")


def replaced_finding():
    f = Finding("p1", OLD, "Стандарты (ГОСТ, поверка)", f"Стандарт {OLD} заменён.", f"Заменить ссылку на {NEW}.", "high",
                ["ROSSTANDART"], False, "rule:gost", 0.9)
    f._replaced = status(OLD)
    return f


@pytest.mark.parametrize("slug,designation", [
    ("gost_34441-2024_mezhgosudarstvennyy_standart_lifty.html", "ГОСТ 34441-2024"),
    ("gost_33984_1-2023_mezhgosudarstvennyy_standart.html", "ГОСТ 33984.1-2023"),
    ("gost_r_53782-2010_nacionalnyy_standart.html", "ГОСТ Р 53782-2010"),
    ("sp_54_13330_2022_svod_pravil.html", "СП 54.13330.2022"),
    ("about.html", None),
])
def test_designation_from_meganorm_slug(slug, designation):
    assert gost_texts.designation_from_slug(slug) == designation


def test_values_and_changes_of_clause():
    assert {(900.0, "мм"), (1100.0, "мм")} <= editions.values("на высоте от 900 до 1 100 мм от пола")
    assert editions.values("по ГОСТ 34441-2024, п. 4.2.1") == set()
    ch = editions.changes("Сигнал должен иметь уровень не менее 35 дБА.", "Сигнал может иметь уровень не менее 40 дБА.")
    assert ch["values_removed"] and ch["values_added"]
    text = editions.describe(ch)
    assert "35" in text and "40" in text and "«должен» → «может»" in text


def test_comparison_aligns_clauses_and_finds_changes(env):
    both_editions(env[0])
    comp = editions.comparison(OLD, NEW)
    s = comp.summary()
    assert s["added"] == 1 and s["changed"] >= 2 and s["unchanged"] >= 1
    numbers = {comp.old[i]["number"]: comp.new[j]["number"] for i, (j, _) in comp.pairs.items()}
    assert numbers["4.1"] == "4.1" and numbers["4.3"] == "4.3"
    cp = editions.counterpart(OLD, "4.1")
    assert cp["status"] == "changed" and "850" in cp["quote"] and "850" in cp["change"]


def test_annotate_reports_mismatch_with_both_quotes(env):
    both_editions(env[0])
    doc = tz("Кнопки вызова на этажах | высота установки от 900 до 1100 мм от уровня пола",
             "Указатель положения кабины | над дверями шахты на основном посадочном этаже")
    f = replaced_finding()
    extra = editions.annotate(doc, [f])
    lines = f.evidence["сверка требований ТЗ с новой редакцией"]
    assert any("4.1" in x and "850" in x for x in lines)
    assert "https://example.org/2020" in f.evidence["новая редакция"]
    assert "Пункты для сверки: п. 4.1" in f.fix and "Сверка с текстом " + NEW in f.explanation
    assert len(extra) == 1 and extra[0].source == "rule:gost-edition" and extra[0].block_id == "t1r1"
    assert "900" in extra[0].evidence["цитата прежней редакции"] and "850" in extra[0].evidence["цитата новой редакции"]
    assert "не соответствует" not in " ".join(x for x in lines if "Указатель" in x)


def test_annotate_says_honestly_when_text_missing(env):
    (env[0] / "old.json").write_text(json.dumps(edition(OLD, "от 900 до 1100 мм", "должен звучать"), ensure_ascii=False), encoding="utf8")
    f = replaced_finding()
    assert editions.annotate(tz("Кнопки вызова | от 900 до 1100 мм"), [f]) == []
    assert "в открытых источниках не найден" in f.explanation and "сверить вручную" in f.explanation
    assert "нет ни в корпусе" in f.evidence["новая редакция"]


class Response:
    def __init__(self, body):
        self.content = body.encode("utf8")
        self.text = body


class FakeHttp:
    offline = False

    def __init__(self, pages):
        self.pages, self.seen = pages, []

    def get(self, url, params=None, **kw):
        self.seen.append(url)
        for k, v in self.pages.items():
            if url.startswith(k):
                return Response(v)
        return Response("<html><body></body></html>")


def meganorm_page(designation):
    parts = [f'<div class="s1">МЕЖГОСУДАРСТВЕННЫЙ СТАНДАРТ {designation}</div>']
    for n in range(1, 9):
        parts.append(f'<div class="s2">{n} Раздел номер {n}</div>')
        parts.append(f'<div class="s3">{"Требования к лифтам и устройствам вызова кабины. " * 8}</div>')
    return "<html><head><meta charset=\"utf-8\"></head><body><div class=\"contener_doc\">" + "".join(parts) + "</div></body></html>"


def test_fetcher_finds_text_on_meganorm_and_saves_corpus_doc(env):
    base = gost_texts.MEGANORM + "gost_gosudarstvennyj-standart/"
    listing = ('<a href="/mega_doc/norm/gost_gosudarstvennyj-standart/0/gost_3999-2020_mezhgosudarstvennyy_standart_lifty.html">x</a>'
               '<a href="gost_gosudarstvennyj-standart_1.html">2</a>')
    http = FakeHttp({base + "gost_gosudarstvennyj-standart_0.html": listing,
                     "https://meganorm.ru/mega_doc/norm/gost_gosudarstvennyj-standart/0/gost_3999-2020": meganorm_page(NEW)})
    rec = gost_texts.Fetcher(http, browser=False, say=lambda *a: None).fetch(NEW, "new", OLD)
    assert rec["ok"] and rec["via"] == "meganorm" and rec["clauses"] >= 6
    with gzip.open(gost_texts.doc_path(NEW), "rt", encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["document"] == NEW and doc["replaces"] == OLD and doc["status"] == "Действует" and doc["blocks"]
    assert gost_texts.Fetcher(http, False, lambda *a: None).meganorm_url(NEW).endswith("lifty.html")


def test_fetcher_reports_every_source_when_text_not_found(env):
    rec = gost_texts.Fetcher(FakeHttp({}), browser=False, say=lambda *a: None).fetch("ГОСТ 1-2099")
    assert not rec["ok"] and rec["note"] == "текст редакции в открытых источниках не найден"
    assert [e.split(":")[0] for e in rec["errors"]] == ["meganorm.ru, страница документа (HTML)", "docs.cntd.ru, полный текст",
                                                       "protect.gost.ru, просмотр страниц и OCR"]


def test_check_text_rejects_fragment():
    with pytest.raises(gost_texts.SourceError, match="только часть документа"):
        gost_texts.check_text(NEW, [{"type": "text", "text": f"ГОСТ {NEW[5:]} Лифты"}, {"type": "text", "text": "1 Область применения"}])
    with pytest.raises(gost_texts.SourceError, match="обозначение"):
        gost_texts.check_text(NEW, [{"type": "text", "text": "ГОСТ 3999-2010 " + "текст " * 600}])


def test_source_offline_keeps_saved_texts(env):
    save_text(edition(NEW, "от 850 до 1100 мм", "может звучать"))
    prev = {NEW: {"designation": NEW, "ok": True, "role": "new", "replaces": OLD, "retrieved": refdata.now_iso()[:10]}}
    src = gost_texts.GostTextsSource(Context(offline=True, browser=False))
    src.targets = lambda prev: [(OLD, NEW)]
    got = src._collect(prev)
    assert got.records[NEW]["ok"]


def test_index_includes_saved_edition_texts(env, tmp_path):
    both_editions(env[0])
    index = tmp_path / "index.json"
    rag.build(env[0], index)
    rows = json.loads(index.read_text(encoding="utf8"))["rows"]
    assert {r["document"] for r in rows} == {OLD, NEW}
    assert all(r.get("replaces") == OLD for r in rows if r["document"] == NEW)
    assert all(r.get("replaced_by") == NEW for r in rows if r["document"] == OLD)


def test_vectors_refresh_encodes_only_new_rows(env, tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    from tzcheck.rag import models
    monkeypatch.setenv("TZCHECK_RAG_VECTOR_DIR", str(tmp_path / "vectors"))
    (env[0] / "old.json").write_text(json.dumps(edition(OLD, "от 900 до 1100 мм", "должен звучать"), ensure_ascii=False), encoding="utf8")
    index = tmp_path / "index.json"
    rag.build(env[0], index)

    class Enc:
        calls = 0

        def encode(self, texts, **kw):
            Enc.calls += len(texts)
            return np.ones((len(texts), 4), dtype="float32")

    rows = rag.Search(index).rows
    texts = [models.prefix(models.DEFAULT_MODEL) + models.doc_text(r) for r in rows]
    models._save(models.DEFAULT_MODEL, np.zeros((len(rows), 4), dtype="float32"), [models.row_key(models.DEFAULT_MODEL, t) for t in texts],
                 {}, index)
    save_text(edition(NEW, "от 850 до 1100 мм", "может звучать"))
    rag.build(env[0], index)
    added = models.refresh(models.DEFAULT_MODEL, Enc(), index)
    assert added == Enc.calls and 0 < added < len(rag.Search(index).rows)
    assert models.load_vectors(models.DEFAULT_MODEL, index).shape[0] == len(rag.Search(index).rows)


def test_requirement_from_other_standard_section_is_dropped():
    class F:
        def __init__(self, topic, items):
            self._edition = {"topic": topic, "items": items}

    item = lambda block, ctx: {"block_id": block, "context": ctx, "dup": False, "how": "BM25 по основам слов", "score": 1.0}  # noqa: E731
    signals = F({"сигнал", "управл"}, [item("t2r17", "Раздел 5. Диспетчеризация."), item("t1r31", "Технические характеристики")])
    dispatch = F({"диспет", "контро"}, [item("t2r18", "Раздел 5. Диспетчеризация."), item("t1r31", "Технические характеристики")])
    editions._dedupe_blocks([signals, dispatch])
    assert signals._edition["items"][0]["dup"] and not dispatch._edition["items"][0]["dup"]
    assert not signals._edition["items"][1]["dup"] and not dispatch._edition["items"][1]["dup"]
