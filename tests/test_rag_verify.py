import json

import tzcheck.rag as rag
from tzcheck.model import Finding
from tzcheck.pii import Scrubber
from tzcheck.rag import enrich, retrieve, verify

from test_rag import law_with_parts, standard, write_corpus, _use_corpus

TM_QUOTE = "в описание предмета закупки не должны включаться требования или указания в отношении товарных знаков"


class Stub:
    """Модель-заглушка: на каждого кандидата решение по функции rule(номер замечания, номер кандидата, текст промпта)."""

    def __init__(self, rule):
        self.rule, self.prompts = rule, []

    def __call__(self, prompt, max_tokens=0):
        self.prompts.append(prompt)
        out = []
        for line in prompt.splitlines():
            line = line.strip()
            if line.startswith(tuple("123456789")) and " (c=" in line:
                n, c = line.split(" ", 1)[0].split(".")
                out.append(dict(n=int(n), c=int(c), **self.rule(int(n), int(c), line)))
        return {"decisions": out}


def brand_finding():
    return Finding('p1', 'Осциллограф RIGOL DHO804 или эквивалент', 'Ограничение конкуренции',
                   'В описании предмета закупки указан товарный знак.', 'Убрать марку')


def test_find_quote_tolerates_spacing_only():
    text = "2) в описание предмета закупки\nне должны включаться   требования или указания в отношении товарных знаков."
    assert verify.find_quote(TM_QUOTE, text) == text[3:-1]
    assert verify.find_quote("в описание предмета закупки не должны включаться указания на марки", text) is None
    assert verify.find_quote("в описание предмета закупки ... товарных знаков", text) is None
    assert verify.find_quote("товарных знаков", text) is None  # короче 6 слов


def test_lanes_bring_law_for_product_query(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts(), standard())
    _use_corpus(monkeypatch, tmp_path, index)
    search = rag._searcher("bm25", "")
    c = retrieve.candidates(search, 'Лифт OTIS или эквивалент', 'Указан товарный знак лифта', 'Ограничение конкуренции',
                            'выполнение работ по замене лифта')
    assert any(h['document'] == '223-ФЗ' and h['number'] == 'Статья 3, ч. 6.1' for h in c)
    assert {h['via'] for h in c} <= {'поиск по законам', 'поиск по стандартам'}
    rag._searcher.cache_clear()


def test_verified_clause_attached_with_exact_quote(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    stub = Stub(lambda n, c, line: {"decision": "applicable", "quote": TM_QUOTE, "reason": "товарный знак"}
                if "ч. 6.1" in line else {"decision": "not_applicable", "quote": "", "reason": "о другом"})
    monkeypatch.setattr("tzcheck.llm._call", stub)
    f = brand_finding()
    enrich([f], scr=Scrubber(), use_llm=True)
    assert f.evidence['пункт нормы'].startswith('223-ФЗ, Статья 3, ч. 6.1')
    assert f.evidence['цитата нормы'] == TM_QUOTE
    assert 'применимость подтвердила модель' in f.evidence['источник нормы']
    assert f.evidence['проверка основания'].startswith('модель: применим')
    rag._searcher.cache_clear()


def test_paraphrased_quote_or_not_applicable_means_no_basis(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    monkeypatch.setattr("tzcheck.llm._call", Stub(lambda n, c, line: {
        "decision": "applicable", "quote": "нельзя указывать товарные знаки и фирменные наименования в описании", "reason": "пересказ"}))
    f = brand_finding()
    enrich([f], scr=Scrubber(), use_llm=True)
    assert 'пункт нормы' not in f.evidence
    assert f.evidence['основание нормы'] == verify.NOT_FOUND
    assert 'цитата модели не найдена' in f.evidence['проверка основания']
    monkeypatch.setattr("tzcheck.llm._call", Stub(lambda n, c, line: {"decision": "not_applicable", "quote": "", "reason": "нет"}))
    g = brand_finding()
    g.norm_ids, g.source = ['223-FZ-3-6.1-2'], 'llm'  # статью выбрала модель: проверяется, как и открытый поиск
    enrich([g], scr=Scrubber(), use_llm=True)
    assert 'пункт нормы' not in g.evidence and g.evidence['основание нормы'] == verify.NOT_FOUND
    rag._searcher.cache_clear()


def test_rule_article_attached_without_model(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    stub = Stub(lambda n, c, line: {"decision": "not_applicable", "quote": "", "reason": "нет"})
    monkeypatch.setattr("tzcheck.llm._call", stub)
    rule = brand_finding()
    rule.norm_ids = ['223-FZ-3-6.1-2']
    enrich([rule], scr=Scrubber(), use_llm=True)
    assert not stub.prompts  # статья из базы норм правила: модель не спрашивается
    assert rule.evidence['пункт нормы'].startswith('223-ФЗ, Статья 3, ч. 6.1')
    assert 'по статье из базы норм' in rule.evidence['источник нормы']
    rag._searcher.cache_clear()


def test_model_failure_falls_back_to_article_without_open_search(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)

    def broken(prompt, max_tokens=0):
        raise RuntimeError("нет сети")
    monkeypatch.setattr("tzcheck.llm._call", broken)
    with_id, open_only = brand_finding(), brand_finding()
    with_id.norm_ids = ['223-FZ-3-6.1-2']
    enrich([with_id, open_only], scr=Scrubber(), use_llm=True)
    assert with_id.evidence['пункт нормы'].startswith('223-ФЗ, Статья 3, ч. 6.1')
    assert 'пункт нормы' not in open_only.evidence
    rag._searcher.cache_clear()


def test_tz_text_is_masked_before_model(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    stub = Stub(lambda n, c, line: {"decision": "unclear", "quote": "", "reason": ""})
    monkeypatch.setattr("tzcheck.llm._call", stub)
    f = Finding('p1', 'Осциллограф RIGOL, звонить ivanov@example.org', 'Ограничение конкуренции', 'Указан товарный знак.', 'x')
    enrich([f], scr=Scrubber(ner=False), use_llm=True)
    assert stub.prompts and 'ivanov@example.org' not in stub.prompts[0]
    rag._searcher.cache_clear()


def test_without_model_open_search_stays_off(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    monkeypatch.delenv('TZCHECK_RAG_OPEN', raising=False)
    f = brand_finding()
    enrich([f])
    assert not f.evidence
    rag._searcher.cache_clear()


def test_chunk_ref_accepted_only_with_corpus_quote(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    hit = next(h for h in rag._searcher("bm25", "").rows if h['number'] == 'Статья 3, ч. 6.1')
    labels = {"R1": rag._public(hit)}
    ok, bad = brand_finding(), brand_finding()
    verify.attach_chunk_ref(ok, {"norm_ref": "R1", "norm_quote": TM_QUOTE}, labels)
    verify.attach_chunk_ref(bad, {"norm_ref": "R1", "norm_quote": "товарные знаки указывать нельзя никогда и нигде вообще"}, labels)
    assert ok.evidence['цитата нормы'] == TM_QUOTE and ok.evidence['проверка основания']
    assert 'пункт нормы' not in bad.evidence and 'не принята' in bad.evidence['ссылка модели на пункт нормы']
    rag._searcher.cache_clear()


def test_subject_of_finds_procurement_subject():
    assert retrieve.subject_of("ТЕХНИЧЕСКОЕ ЗАДАНИЕ Предмет закупки: выполнение работ по замене грузового лифта. Условия") \
        == "выполнение работ по замене грузового лифта"
    assert retrieve.subject_of("", "Техническое задание на поставку измерительных приборов для лаборатории") \
        .startswith("поставку измерительных приборов")
    assert json.dumps(retrieve.queries("q", "e", "Ограничение конкуренции", "s"), ensure_ascii=False).count("товарные знаки") == 1


def test_tasks_skipped_in_batch_are_asked_again():
    hit = {"id": "x:1", "document": "223-ФЗ", "number": "Статья 3, ч. 6.1", "title": "", "text": "текст", "source": ""}
    tasks = [{"quote": f"цитата {i}", "explanation": "", "category": "Ограничение конкуренции", "subject": "", "candidates": [hit]}
             for i in range(3)]
    calls = []

    def lazy(prompt, max_tokens=0):  # в пачке отвечает только на первое замечание
        calls.append(prompt)
        return {"decisions": [{"n": 1, "c": 1, "decision": "not_applicable", "quote": "", "reason": "о другом"}]}
    out = verify.judge(tasks, Scrubber(), call=lazy, batch=3)
    assert all(o[0]["decision"] == "not_applicable" for o in out)
    assert len(calls) == 3  # пачка и два повтора по одному
