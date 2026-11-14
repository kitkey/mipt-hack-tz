import docx

from tzcheck.parse import parse


def _doc(tmp_path, rows, nested=None):
    p = tmp_path / "t.docx"
    d = docx.Document()
    t = d.add_table(rows=len(rows), cols=len(rows[0]))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    if nested:
        inner = t.cell(*nested[0]).add_table(rows=len(nested[1]), cols=len(nested[1][0]))
        for r, row in enumerate(nested[1]):
            for c, v in enumerate(row):
                inner.cell(r, c).text = v
    d.add_paragraph("Текст после таблицы")
    d.add_table(rows=1, cols=2).cell(0, 0).text = "вторая"
    d.save(p)
    return parse(str(p))


def test_empty_and_equal_cells_are_kept(tmp_path):
    doc = _doc(tmp_path, [["Наименование", "Мин", "Макс", "Ед."], ["Длина", "", "", "мм"], ["Ширина", "5", "5", "мм"]])
    rows = [b for b in doc.blocks if b.kind == "row" and b.table == 0]
    assert rows[1].grid == ["Длина", "", "", "мм"]
    assert rows[2].cells == ["Ширина", "5", "5", "мм"]


def test_horizontal_and_vertical_merge_flags(tmp_path):
    p = tmp_path / "m.docx"
    d = docx.Document()
    t = d.add_table(rows=3, cols=3)
    t.cell(0, 0).merge(t.cell(0, 1)).text = "Шапка"
    t.cell(0, 2).text = "Кол-во"
    t.cell(1, 0).merge(t.cell(2, 0)).text = "Позиция"
    for r in (1, 2):
        t.cell(r, 1).text = f"х{r}"
        t.cell(r, 2).text = str(r)
    d.save(p)
    rows = [b for b in parse(str(p)).blocks if b.kind == "row"]
    assert rows[0].grid == ["Шапка", "Шапка", "Кол-во"] and rows[0].hmerge == [False, True, False]
    assert rows[2].grid[0] == "Позиция" and rows[2].vmerge == [True, False, False]
    assert rows[1].vmerge == [False, False, False]


def test_nested_table_rows_follow_parent_and_keep_numbering(tmp_path):
    doc = _doc(tmp_path, [["Наименование", "Характеристики"], ["Насос", ""]], nested=((1, 1), [["Напор", "10 м"], ["Расход", "5 м3/ч"]]))
    ids = [b.id for b in doc.blocks if b.kind == "row"]
    assert ids == ["t1r1", "t1r2", "t1r2n1r1", "t1r2n1r2", "t2r1"]
    inner = doc.block("t1r2n1r1")
    assert inner.parent == "t1r2" and inner.text == "Напор | 10 м"
    assert "вложенная таблица 1" in inner.location()
