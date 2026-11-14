import pytest


@pytest.fixture(autouse=True)
def _no_real_pii_ner(monkeypatch):
    """Заглушки моделей вида stub/model без api_base считаются внешними: тесты не грузят настоящий NER и не упираются
    в запрет внешней отправки. Тесты слоя ПД и запрета ставят эти переменные сами."""
    monkeypatch.setenv("TZCHECK_PII_NER", "0")
    monkeypatch.setenv("TZCHECK_DOC_PUBLIC", "1")
