from tzcheck.parse import parse_many
from tzcheck.pipeline import check
from tzcheck.reconcile import extract, reconcile
from tzcheck.report import to_html


def test_attachments_and_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    a, b = tmp_path / "main.txt", tmp_path / "app.txt"
    a.write_text("Грузоподъемность: 1000 кг\n", encoding="utf-8")
    b.write_text("Грузоподъемность: 1200 кг\n", encoding="utf-8")
    result = check([a, b], use_llm=False)
    f = next(f for f in result["findings"] if f["source"] == "reconcile")
    assert f["quote"] == "Грузоподъемность: 1000 кг"
    assert f["evidence"]["вторая цитата"] == "Грузоподъемность: 1200 кг"
    assert f["also_in"] == ["a1_p1"]
    assert "app.txt" in to_html(result)


def test_compatible_units_and_intervals(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("Длина: 1000 мм\nДлина: 1 м\nШирина: от 2 до 4 м\nШирина: 3 м\n", encoding="utf-8")
    doc = parse_many([p])
    assert len(extract(doc)) == 4
    assert not reconcile(doc)


def test_missing_reference(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("См. приложение 3\nСм. п. 5.4\n", encoding="utf-8")
    found = reconcile(parse_many([p]))
    assert len(found) == 1 and len(found[0].also_in) == 1
    assert "приложение 3" in found[0].explanation and "п. 5.4" in found[0].explanation


def test_reference_defined_in_same_cell(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("Срок выполнения работ по п. 3.2 настоящего задания. 3.2 Работы выполняются в два этапа.\n", encoding="utf-8")
    assert not reconcile(parse_many([p]))


def test_same_name_rows_with_different_specs_are_different_items(tmp_path):
    import docx
    p = tmp_path / "spec.docx"
    d = docx.Document()
    t = d.add_table(rows=3, cols=3)
    for r, cells in enumerate([("Наименование", "Характеристики", "Количество"),
                               ("Выключатель автоматический", "Номинальный ток 16 А", "4"),
                               ("Выключатель автоматический", "Номинальный ток 25 А", "2")]):
        for c, v in enumerate(cells):
            t.cell(r, c).text = v
    d.save(p)
    assert not [f for f in reconcile(parse_many([p])) if "количество" in f.explanation]


def test_spec_row_with_okpd_code_is_not_toc_entry(tmp_path):
    p = tmp_path / "doc.txt"
    p.write_text("СОДЕРЖАНИЕ\n27.33.13.120 Розетка силовая, ток не более 10 А 5\n", encoding="utf-8")
    assert not [f for f in reconcile(parse_many([p])) if "из содержания" in f.explanation]


def test_xlsx_source(tmp_path):
    import openpyxl
    p = tmp_path / "app.xlsx"
    w = openpyxl.Workbook(); w.active.append(["Масса", "12 кг"]); w.save(p)
    d = parse_many([p])
    assert extract(d)[0].parameter == "масса"
    assert "файл" not in d.blocks[0].location()
    main = tmp_path / "main.txt"
    main.write_text("Поставка весов\n", encoding="utf-8")
    d = parse_many([main, p])
    assert "app.xlsx" in d.blocks[-1].location()


def test_semantic_matching_requires_real_disjoint_evidence(tmp_path, monkeypatch):
    from tzcheck import llm
    from tzcheck.pii import Scrubber
    from tzcheck.reconcile import reconcile_semantic
    p = tmp_path / "doc.txt"
    p.write_text("Масса: 10 кг\nВес: 20 кг\n", encoding="utf-8")
    monkeypatch.setattr(llm, "_call", lambda *args: {"pairs": [[0, 1], [0, 999]]})
    result = reconcile_semantic(parse_many([p]), Scrubber())
    assert len(result) == 1
    assert result[0].needs_review
    assert result[0].evidence["вторая цитата"] == "Вес: 20 кг"


def test_comments_on_both_documents(tmp_path, monkeypatch):
    import docx
    from tzcheck.report import write_all
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    paths = [tmp_path / "main.docx", tmp_path / "attachment.docx"]
    for p, text in zip(paths, ["Масса: 10 кг", "Масса: 20 кг"]):
        d = docx.Document(); d.add_paragraph(text); d.save(p)
    result = check(paths, use_llm=False)
    output = write_all(result, str(paths[0]), str(tmp_path / "out"))
    assert "docx" in output and "docx_attachment_1" in output
    for key in ("docx", "docx_attachment_1"):
        d = docx.Document(output[key])
        assert any("Масса: 10 кг" in c.text or "Масса: 20 кг" in c.text for c in d.comments)


def test_graph_separates_objects_and_conditions(tmp_path):
    from tzcheck.reconcile import build_graph
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("Объект: насос А\nМасса: 10 кг\nМасса: 20 кг\nОбъект: насос Б\nМасса: 30 кг\n", encoding="utf-8")
    b.write_text("Объект: насос А\nМасса: 10 кг при температуре 20 градусов\nМасса: 30 кг при температуре 40 градусов\n", encoding="utf-8")
    d = parse_many([a,b]); graph = build_graph(d)
    assert len([e for e in graph.edges if e["relation"] == "conflict"]) == 1
    assert len([f for f in reconcile(d) if f.category == "Противоречие"]) == 1


def test_completeness_and_acceptance(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("В комплект входит: кабель питания.\nНе входит в комплект: кабель питания.\nФорма приемки: только в электронном виде.\nФорма приемки: только в бумажном виде.\n", encoding="utf-8")
    fs = reconcile(parse_many([p]))
    assert any("компонент" in f.explanation.lower() for f in fs)
    assert any("форма приемки" in f.explanation.lower() for f in fs)


def test_contents_and_repeated_sections(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("СОДЕРЖАНИЕ\n1 Общие положения .... 2\n2 Условия гарантии .... 3\n1 Общие положения\nПоложения об объекте и его комплектации.\n3 Требования качества\nПервое требование качества достаточно подробно.\n4 Требования качества\nВторое требование качества изложено по другому.\n", encoding="utf-8")
    d = parse_many([p]); fs = reconcile(d)
    assert any("содержания" in f.explanation for f in fs)
    assert any("одинаковым названием" in f.explanation for f in fs)


def test_dates_and_deadline_bounds(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("Срок поставки: 01.05.2027\nСрок поставки: 02.05.2027\n", encoding="utf-8")
    assert any(f.category == "Противоречие" for f in reconcile(parse_many([p])))
    p.write_text("Срок поставки: не позднее 01.05.2027\nСрок поставки: не позднее 02.05.2027\n", encoding="utf-8")
    assert not any(f.category == "Противоречие" for f in reconcile(parse_many([p])))


def test_external_regulation_is_not_internal_missing_reference(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("Согласно п. 2.13.2 Положения о закупке.\n", encoding="utf-8")
    assert not reconcile(parse_many([p]))


def test_specification_quantity_tracks_item(tmp_path):
    import docx
    from tzcheck.reconcile import build_graph
    main, app = tmp_path / "main.docx", tmp_path / "app.docx"
    d = docx.Document(); d.add_paragraph("Объект: насос альфа"); d.add_paragraph("Количество: 3 шт"); d.save(main)
    d = docx.Document(); table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Наименование"; table.rows[0].cells[1].text = "Количество"
    cells=table.add_row().cells; cells[0].text="насос альфа"; cells[1].text="2"
    cells=table.add_row().cells; cells[0].text="насос бета"; cells[1].text="7"
    d.save(app); graph=build_graph(parse_many([main,app]))
    conflicts=[e for e in graph.edges if e["relation"] == "conflict"]
    assert len(conflicts) == 1
    assert graph.requirements[conflicts[0]["to"]].object == "насос альфа"


def test_missing_and_existing_section_and_table_links(tmp_path):
    p=tmp_path / "doc.txt"
    p.write_text("1 Общие положения\nСм. раздел 1\nСм. раздел 8\nСм. таблицу 3\n", encoding="utf-8")
    fs=reconcile(parse_many([p]))
    assert len(fs) == 1 and {fs[0].quote} <= {"См. раздел 8", "См. таблицу 3"}
    assert "раздел 8" in fs[0].explanation and "таблицу 3" in fs[0].explanation


def test_speed_unit_does_not_become_length(tmp_path):
    p=tmp_path / "doc.txt"
    p.write_text("Скорость: 2 м/с\nСкорость: 3 м/с\n", encoding="utf-8")
    requirements=extract(parse_many([p]))
    assert [r.unit for r in requirements] == ["speed", "speed"]
    assert len(reconcile(parse_many([p]))) == 1


def test_semantic_matching_covers_requirements_past_first_batch(tmp_path, monkeypatch):
    from tzcheck import llm
    from tzcheck.pii import Scrubber
    from tzcheck.reconcile import reconcile_semantic
    p=tmp_path / "doc.txt"
    lines=[f"Показатель {chr(1072+i//32)}{chr(1072+i%32)}: {i+1} кг" for i in range(81)]
    p.write_text("\n".join(lines), encoding="utf-8")
    calls=[]
    def fake(prompt, *args):
        calls.append(prompt)
        return {"pairs": [[0,80]]} if "\n0:" in prompt and "\n80:" in prompt else {"pairs": []}
    monkeypatch.setattr(llm, "_call", fake)
    findings=reconcile_semantic(parse_many([p]), Scrubber())
    assert len(calls) == 6
    assert len(findings) == 1 and findings[0].evidence["вторая цитата"] == lines[80]


def test_characteristic_table_has_unit_in_separate_column(tmp_path):
    import docx
    p=tmp_path / "app.docx"; d=docx.Document(); t=d.add_table(rows=1,cols=3)
    for c,v in zip(t.rows[0].cells,["Наименование характеристики", "Единица измерения", "Значение"]): c.text=v
    for value in ("Не менее 2", "Не более 1"):
        for c,v in zip(t.add_row().cells,["Количество приемников", "Штука", value]): c.text=v
    d.save(p); parsed=parse_many([p]); reqs=extract(parsed)
    assert len(reqs)==2 and all(r.unit == "count" for r in reqs)
    assert len(reconcile(parsed))==1


def test_characteristic_table_tracks_product_groups(tmp_path):
    import docx
    p=tmp_path / "app.docx"; d=docx.Document(); t=d.add_table(rows=1,cols=3)
    for c,v in zip(t.rows[0].cells,["Наименование характеристики", "Единица измерения", "Значение"]): c.text=v
    rows=[("Микрометр 0-25 мм", "Штука", "1"), ("Шаг дискретности", "Миллиметр", "0.001"),
          ("Микрометр 25-50 мм", "Штука", "1"), ("Шаг дискретности", "Миллиметр", "0.01")]
    for row in rows:
        for c,v in zip(t.add_row().cells,row): c.text=v
    d.save(p); parsed=parse_many([p])
    assert len(extract(parsed))==4
    assert not reconcile(parsed)


def test_bracketed_ranges_from_tables(tmp_path):
    import docx
    p=tmp_path / "app.docx"; d=docx.Document(); t=d.add_table(rows=1,cols=3)
    for c,v in zip(t.rows[0].cells,["Наименование характеристики", "Единица измерения", "Значение"]): c.text=v
    for value in ("[0-25]", "[30-50]"):
        for c,v in zip(t.add_row().cells,["Диапазон измерений", "Миллиметр", value]): c.text=v
    d.save(p); reqs=extract(parse_many([p]))
    assert len(reqs) == 2 and reqs[0].low == 0 and reqs[0].high == .025
    assert len(reconcile(parse_many([p]))) == 1


def test_specification_uses_name_instead_of_position_number(tmp_path):
    import docx
    p=tmp_path / "app.docx"; d=docx.Document(); t=d.add_table(rows=1,cols=3)
    for c,v in zip(t.rows[0].cells,["Номер позиции товара", "Наименование товара", "Количество товара"]): c.text=v
    for row in [("10", "Насос альфа", "1"), ("11", "Насос бета", "4")]:
        for c,v in zip(t.add_row().cells,row): c.text=v
    d.save(p); reqs=extract(parse_many([p]))
    assert [r.object for r in reqs] == ["насос альфа", "насос бета"]
    assert not reconcile(parse_many([p]))
