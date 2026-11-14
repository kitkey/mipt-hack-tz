import zipfile

import docx

from tzcheck.report import to_docx_comments


def _finding(block_id, quote):
    return {"id": "F001", "block_id": block_id, "quote": quote, "category": "Некорректная характеристика", "severity": "medium",
            "needs_review": False, "explanation": "Нет единицы измерения.", "fix": "Указать единицу.", "norm_ids": [], "evidence": {},
            "location": "таблица 1, строка 1"}


def test_comment_lands_in_nested_table_row(tmp_path):
    d = docx.Document()
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "Позиция 1"
    inner = t.cell(0, 1).add_table(rows=2, cols=1)
    inner.cell(0, 0).text = "Характеристика"
    inner.cell(1, 0).text = "Мощность не менее 5"
    src, out = tmp_path / "nested.docx", tmp_path / "out.docx"
    d.save(src)
    res = {"_doc": object(), "_paths": [str(src)], "findings": [_finding("t1r1n1r2", "Мощность не менее 5"), _finding("t1r1n9r9", "нет такой")]}
    assert to_docx_comments(res, str(src), str(out)) is True
    with zipfile.ZipFile(out) as z:
        assert "Нет единицы измерения" in z.read("word/comments.xml").decode("utf-8")
        body = z.read("word/document.xml").decode("utf-8")
    assert body.index("commentRangeStart") < body.index("Мощность не менее 5")


def test_unknown_row_id_is_skipped_without_error(tmp_path):
    d = docx.Document()
    d.add_table(rows=1, cols=1).cell(0, 0).text = "Строка"
    src, out = tmp_path / "plain.docx", tmp_path / "out.docx"
    d.save(src)
    res = {"_doc": object(), "_paths": [str(src)], "findings": [_finding("t1rXr2", "Строка")]}
    assert to_docx_comments(res, str(src), str(out)) is False
