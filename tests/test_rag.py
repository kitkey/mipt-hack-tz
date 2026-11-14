import json

import pytest

import tzcheck.rag as rag
from tzcheck.model import Finding
from tzcheck.rag import Search, best_quote, build, chunks, enrich, mentioned_documents


def fixture():
    return {'id': 'test', 'document': '102-ФЗ', 'edition': 'тест', 'source': 'https://example.org', 'blocks': [
        {'type': 'text', 'text': 'Статья 13. Поверка средств измерений'},
        {'type': 'text', 'text': 'Средства измерений проходят первичную поверку до ввода в эксплуатацию.'},
        {'type': 'table', 'text': 'Параметр | Значение\nДопуск | один'},
        {'type': 'text', 'text': 'Статья 14. Следующая статья'},
        {'type': 'text', 'text': 'Иной текст для проверки разделения статей документа.'}]}


def law_with_parts():
    return {'id': 'fz', 'document': '223-ФЗ', 'edition': 'тест', 'source': 'https://example.org/223', 'blocks': [
        {'type': 'text', 'text': 'Статья 3. Принципы и основные положения закупки товаров, работ, услуг'},
        {'type': 'text', 'text': '1. При закупке товаров заказчики руководствуются принципами равноправия и справедливости.'},
        {'type': 'text', 'text': '6.1. При описании в документации о конкурентной закупке предмета закупки заказчик должен руководствоваться правилами:'},
        {'type': 'text', 'text': '2) в описание предмета закупки не должны включаться требования или указания в отношении товарных знаков, '
                                 'знаков обслуживания, фирменных наименований, патентов, полезных моделей.'},
        {'type': 'text', 'text': '7. При закупке заказчик вправе установить требование об отсутствии сведений в реестре недобросовестных поставщиков.'}]}


def standard():
    return {'id': 'gost', 'document': 'ГОСТ Р 53782-2010', 'edition': '2010', 'source': 'https://example.org/g', 'status': 'Действует', 'blocks': [
        {'type': 'text', 'text': '1 Область применения'},
        {'type': 'text', 'text': 'Настоящий стандарт устанавливает правила и методы оценки соответствия лифтов при вводе в эксплуатацию.'},
        {'type': 'text', 'text': '5.2 Полное техническое освидетельствование'},
        {'type': 'text', 'text': 'Полное техническое освидетельствование лифта проводят после монтажа до ввода лифта в эксплуатацию.'}]}


def write_corpus(tmp_path, *docs):
    for d in docs:
        (tmp_path / (d['id'] + '.json')).write_text(json.dumps(d, ensure_ascii=False), encoding='utf8')
    index = tmp_path / 'index.json'
    build(tmp_path, index)
    return index


def test_chunks_keep_clauses_and_tables():
    rows = chunks(fixture())
    assert [r['kind'] for r in rows] == ['clause', 'table', 'clause']
    assert rows[0]['number'] == 'Статья 13' and 'поверку' in rows[0]['text']
    assert rows[1]['number'] == 'Статья 13' and 'Допуск' in rows[1]['text']
    assert 'Следующая' not in rows[0]['text']


def test_law_articles_split_into_parts():
    rows = chunks(law_with_parts())
    numbers = [r['number'] for r in rows]
    assert 'Статья 3, ч. 6.1' in numbers and 'Статья 3, ч. 7' in numbers
    part = next(r for r in rows if r['number'] == 'Статья 3, ч. 6.1')
    assert 'товарных знаков' in part['text'] and part['article'] == 'Статья 3'
    assert 'реестре недобросовестных' not in part['text']


def test_long_clause_is_windowed_and_windows_are_substrings():
    doc = law_with_parts()
    doc['blocks'].insert(3, {'type': 'text', 'text': ' '.join(['Длинное положение о закупке номер %d.' % i for i in range(300)])})
    rows = [r for r in chunks(doc) if r['number'] == 'Статья 3, ч. 6.1']
    assert len(rows) > 1 and all(len(r['text']) <= 2200 for r in rows)


def test_search_and_rejection(tmp_path):
    index = write_corpus(tmp_path, fixture())
    s = Search(index)
    assert s.search('первичная поверка средства измерения до эксплуатации', threshold=0)[0]['number'] == 'Статья 13'
    assert not s.search('квантовый бегемот на марсе')


def test_explicit_unknown_standard_refuses_even_with_common_terms(tmp_path):
    index = write_corpus(tmp_path, fixture())
    assert not Search(index).search('ГОСТ 987654-2026 первичная поверка средства измерения до эксплуатации', threshold=0)


def test_designation_without_r_maps_to_national_standard(tmp_path):
    index = write_corpus(tmp_path, standard())
    rows = Search(index).rows
    assert mentioned_documents('Работы по ГОСТ 53782-2010', rows) == {'ГОСТ Р 53782-2010'}
    assert mentioned_documents('Работы по ГОСТ Р 53782-2010', rows) == {'ГОСТ Р 53782-2010'}


def test_best_quote_is_exact_substring_and_relevant():
    text = ('6.1. При описании предмета закупки заказчик руководствуется правилами:\n'
            '1) указываются функциональные характеристики;\n'
            '2) не должны включаться требования или указания в отношении товарных знаков, фирменных наименований.')
    q = best_quote(text, 'указан товарный знак и фирменное наименование')
    assert q in text and 'товарных знаков' in q


def test_disabled_or_missing_index_does_not_change_finding(monkeypatch, tmp_path):
    f = Finding('p1', 'цитата', 'Гарантийные условия', 'объяснение', 'исправление', norm_ids=['GK-470'])
    monkeypatch.setenv('TZCHECK_RAG', '1')
    monkeypatch.setattr(rag, 'INDEX', tmp_path / 'missing')
    rag._searcher.cache_clear()  # поисковик по настоящему индексу из прошлых тестов не должен подменять отсутствующий
    enrich([f])
    assert not f.evidence
    monkeypatch.setenv('TZCHECK_RAG', '0')
    enrich([f])
    assert not f.evidence


def _use_corpus(monkeypatch, tmp_path, index):
    monkeypatch.setattr(rag, 'INDEX', index)
    monkeypatch.setattr(rag, 'CORPUS', tmp_path)
    monkeypatch.setenv('TZCHECK_RAG', '1')
    monkeypatch.setenv('TZCHECK_RAG_MODE', 'bm25')
    rag._searcher.cache_clear()


def test_enrich_attaches_law_part_with_verified_quote(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    f = Finding('p1', 'Осциллограф RIGOL DHO804 или эквивалент', 'Ограничение конкуренции',
                'В описании предмета закупки указан товарный знак.', 'Убрать марку', norm_ids=['223-FZ-3-6.1-2'])
    enrich([f])
    assert f.evidence['пункт нормы'].startswith('223-ФЗ, Статья 3, ч. 6.1')
    assert 'товарных знаков' in f.evidence['цитата нормы']
    assert 'по статье из базы норм' in f.evidence['источник нормы']
    rag._searcher.cache_clear()


def test_enrich_refuses_when_source_corpus_lost_quote(monkeypatch, tmp_path):
    index = write_corpus(tmp_path, law_with_parts())
    _use_corpus(monkeypatch, tmp_path, index)
    (tmp_path / 'fz.json').write_text(json.dumps({'id': 'fz', 'document': '223-ФЗ', 'edition': 'x', 'source': 'https://example.org/223',
                                                  'blocks': []}), encoding='utf8')
    f = Finding('p1', 'RIGOL DHO804', 'Ограничение конкуренции', 'указан товарный знак', 'x', norm_ids=['223-FZ-3-6.1-2'])
    enrich([f])
    assert 'пункт нормы' not in f.evidence
    rag._searcher.cache_clear()


def test_enrich_by_standard_designation_and_replaced_flag(monkeypatch, tmp_path):
    doc = standard()
    doc['status'] = 'Заменён'
    monkeypatch.setattr(rag.refdata, 'gost_status', lambda d: None)
    index = write_corpus(tmp_path, doc)
    _use_corpus(monkeypatch, tmp_path, index)
    f = Finding('p1', 'Освидетельствование по ГОСТ Р 53782-2010', 'Стандарты (ГОСТ, поверка)',
                'полное техническое освидетельствование лифта до ввода в эксплуатацию', 'x')
    enrich([f])
    assert f.evidence['пункт нормы'].startswith('ГОСТ Р 53782-2010, п. 5.2')
    assert f.evidence.get('стандарт заменён') and f.needs_review
    rag._searcher.cache_clear()


def test_hybrid_uses_both_ranks_and_rejects_stale_vectors(tmp_path, monkeypatch):
    np = pytest.importorskip('numpy')
    import tzcheck.rag.models as models
    index = write_corpus(tmp_path, fixture())
    monkeypatch.setattr(models, 'INDEX', index)
    monkeypatch.setenv('TZCHECK_RAG_VECTOR_DIR', str(tmp_path))
    np.save(str(tmp_path / 'test--model') + '.npy', np.array([[1, 0], [0, 1], [0, 1]], dtype='float32'))
    (tmp_path / 'test--model.json').write_text(json.dumps({'fingerprint': models.fingerprint(index)}), encoding='utf8')

    class Dummy:
        def encode(self, *args, **kwargs):
            return np.array([[1, 0]], dtype='float32')

    monkeypatch.setattr(models, 'encoder', lambda model, device=None: Dummy())
    search = models.HybridSearch('test/model', mode='hybrid', index=index)
    assert search.search('первичная поверка средств измерений в эксплуатации', threshold=0)[0]['number'] == 'Статья 13'
    assert not search.search('ГОСТ 987654-2026 поверка средств измерений')
    index.write_text(index.read_text(encoding='utf8') + ' ', encoding='utf8')
    with pytest.raises(ValueError):
        models.HybridSearch('test/model', index=index)


def test_rrf_prefers_items_ranked_high_in_both():
    from tzcheck.rag.models import rrf
    assert rrf([[1, 2, 3], [2, 1, 4]])[:2] in ([1, 2], [2, 1])
    assert rrf([[5, 6], [6, 7]])[0] == 6


def test_item_quote_picks_named_subitem():
    from tzcheck.rag import item_quote
    text = '10. В документации указываются:\n1) требования к безопасности и качеству товара;\n7) порядок формирования цены договора;'
    assert item_quote(text, ['223-FZ-4-10-1']).startswith('1) требования к безопасности')
    assert item_quote(text, ['223-FZ-4-10-7']).startswith('7) порядок')
    assert item_quote(text, ['223-FZ-4-10']) == ''


def test_status_finding_gets_scope_clause_or_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(rag.refdata, 'gost_status', lambda d: None)
    index = write_corpus(tmp_path, standard())
    _use_corpus(monkeypatch, tmp_path, index)
    f = Finding('p1', 'ГОСТ Р 53782-2010', 'Стандарты (ГОСТ, поверка)', 'Стандарт ГОСТ Р 53782-2010 заменён', 'x', source='rule:gost')
    enrich([f])
    assert f.evidence['пункт нормы'].startswith('ГОСТ Р 53782-2010, п. 1')
    doc = standard()
    doc['blocks'] = doc['blocks'][2:]
    index = write_corpus(tmp_path, doc)
    _use_corpus(monkeypatch, tmp_path, index)
    g = Finding('p1', 'ГОСТ Р 53782-2010', 'Стандарты (ГОСТ, поверка)', 'Стандарт ГОСТ Р 53782-2010 заменён', 'x', source='rule:gost')
    enrich([g])
    assert 'пункт нормы' not in g.evidence
    rag._searcher.cache_clear()


def test_open_search_off_by_default(monkeypatch):
    class Lexical:
        pass

    class Hybrid:
        mode = 'hybrid'

    monkeypatch.delenv('TZCHECK_RAG_OPEN', raising=False)
    assert not rag.open_search_allowed(Lexical()) and not rag.open_search_allowed(Hybrid())
    monkeypatch.setenv('TZCHECK_RAG_OPEN', 'auto')
    # auto: открытый поиск только с проверкой кандидатов моделью (tzcheck/rag/verify.py), режим поиска не важен
    assert not rag.open_search_allowed(Hybrid()) and rag.open_search_allowed(Lexical(), verified=True)
    monkeypatch.setenv('TZCHECK_RAG_OPEN', '0')
    assert not rag.open_search_allowed(Hybrid())
    monkeypatch.setenv('TZCHECK_RAG_OPEN', '1')
    assert rag.open_search_allowed(Lexical())
