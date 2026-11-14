import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("eval_labels_mod", os.path.join(ROOT, "eval", "eval_labels.py"))


def _mod():
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_exact_rejects_different_parameter():
    m = _mod()
    assert not m.quote_exact({"quote": "Гарантийный срок не менее 12 месяцев"}, "Срок хранения не менее 24 месяцев")


def test_exact_accepts_short_quote_inside_place():
    m = _mod()
    assert m.quote_exact({"quote": "Mitutoyo"}, "Микрометр Mitutoyo 103-137 | шт | 2")


def test_exact_accepts_long_common_fragment():
    m = _mod()
    assert m.quote_exact({"quote": "Срок хранения не менее 24 месяцев"}, "4.2 Срок хранения не менее 24 месяцев с даты поставки")
