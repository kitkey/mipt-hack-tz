import pytest

import tzcheck.ocr as ocr
from tzcheck.parse import ParseError, parse


def test_mode_values(monkeypatch):
    for value, expected in (("1", "on"), ("on", "on"), ("0", "off"), ("auto", "auto"), ("", "auto")):
        monkeypatch.setenv("TZCHECK_OCR", value)
        assert ocr.mode() == expected
    monkeypatch.setenv("TZCHECK_OCR", "0")
    assert not ocr.enabled()
    monkeypatch.setenv("TZCHECK_OCR", "auto")
    monkeypatch.setattr(ocr, "available", lambda: False)
    assert not ocr.enabled()


def test_boxes_grouped_into_reading_order_and_table_columns():
    boxes = [
        (400, 100, 520, 120, "Значение", 0.99),
        (10, 102, 120, 121, "Параметр", 0.99),
        (10, 40, 300, 60, "Техническое задание", 0.99),
        (10, 160, 90, 180, "Срок", 0.98),
        (95, 161, 260, 181, "поставки 30 дней", 0.97),
    ]
    assert ocr.lines_from_boxes(boxes) == ["Техническое задание", "Параметр | Значение", "Срок поставки 30 дней"]
    assert ocr.lines_from_boxes([]) == []


def _png(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), False)
    pix.clear_with(255)
    path = tmp_path / "scan.png"
    pix.save(str(path))
    return path


def test_image_goes_through_ocr_with_paragraphs_and_table(monkeypatch, tmp_path):
    path = _png(tmp_path)
    monkeypatch.setenv("TZCHECK_OCR", "1")
    monkeypatch.setenv("TZCHECK_OCR_ISOLATE", "0")
    lines = [("Техническое задание на поставку весов", 40, 40, 900, 70),
             ("Срок поставки 30 календарных дней с даты", 40, 100, 1500, 130),
             ("заключения договора.", 40, 140, 500, 170)]
    tables = [{"y": 300, "rows": [["№", "Наименование", "Код ОКПД 2"], ["1", "Весы лабораторные", "26.51.32.110"]]}]
    monkeypatch.setattr(ocr, "_layout_here", lambda img: (lines, tables))
    doc = parse(str(path))
    assert doc.source_format == "png" and doc.name == "scan.png"
    assert any(b.kind == "para" and "30 календарных дней с даты заключения договора." in b.text for b in doc.blocks)
    rows = [b for b in doc.blocks if b.kind == "row"]
    assert rows and rows[-1].cells == ["1", "Весы лабораторные", "26.51.32.110"]
    assert any("распознан OCR" in w for w in doc.warnings)


def test_table_grid_and_rows_from_scan_lines():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    img = np.full((400, 1000, 3), 255, dtype=np.uint8)
    for y in (50, 150, 250, 350):
        img[y:y + 3, 50:950] = 0
    for x in (50, 400, 950):
        img[50:353, x:x + 3] = 0
    img[150:353, 700:703] = 0
    cells = ocr.table_grid(img)
    assert len(cells) == 1 and len(cells[0]) == 8
    boxes = [(60, 90, 300, 110, "Наименование", 0.99), (420, 90, 600, 110, "Характеристики", 0.99),
             (60, 190, 300, 210, "Весы", 0.99), (420, 190, 600, 210, "до 200 г", 0.99), (720, 190, 900, 210, "шт.", 0.99),
             (60, 290, 300, 310, "Гири", 0.99), (420, 290, 600, 310, "класс E2", 0.99), (720, 290, 900, 310, "компл.", 0.99)]
    rows, used = ocr.table_rows(cells[0], boxes)
    assert rows == [["Наименование", "Характеристики"], ["Весы", "до 200 г", "шт."], ["Гири", "класс E2", "компл."]]
    assert used == set(range(8))


def test_scan_without_ocr_reports_clearly(monkeypatch, tmp_path):
    path = _png(tmp_path)
    monkeypatch.setenv("TZCHECK_OCR", "0")
    with pytest.raises(ParseError):
        parse(str(path))


def test_text_pdf_repeats_vertically_merged_cell_like_docx():
    from pathlib import Path
    pdf = Path("samples/pdf/metro_src.pdf")
    if not pdf.exists():
        pytest.skip("нет образца PDF")
    rows = {b.id: b for b in parse(str(pdf)).blocks if b.kind == "row"}
    assert "Московский физико-технический" in rows["t2r5"].cells[1]
    assert "Московский физико-технический" in rows["t2r15"].cells[1]
    assert rows["t2r15"].cells[2] == "26.51.43.190"


def test_rotated_scan_lines_stay_in_order():
    import math
    angle = math.radians(2.5)

    def quad(x0, y0, x1, y1):
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [(x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)) for x, y in pts]

    lines = [("Подрядчик должен выполнить весь комплекс работ", 40), ("Техническим заданием и согласованной сметой", 80),
             ("Пропуск рабочих в здание разрешается по списку", 120), ("утверждённому Заказчиком при предъявлении", 160)]
    polys, texts = [], []
    for text, y in lines:
        polys += [quad(20, y, 900, y + 22), quad(1000, y, 1400, y + 22)]
        texts += [text, "колонка"]
    result = {"rec_texts": texts, "rec_scores": [0.99] * len(texts), "rec_polys": polys}
    assert abs(math.degrees(ocr.skew_angle(polys)) - 2.5) < 0.1
    got = ocr.lines_from_boxes(ocr._boxes(result))
    assert got == [f"{t} | колонка" for t, _ in lines]


def test_latin_lookalikes_in_russian_lines_are_fixed_but_brands_kept():
    assert ocr.fix_homoglyphs("согласованию c Заказчиком, ha объекте") == "согласованию с Заказчиком, на объекте"
    assert ocr.fix_homoglyphs("Пропуск рабочих B здание по списку") == "Пропуск рабочих В здание по списку"
    assert ocr.fix_homoglyphs("действующими chип и саnпиh") == "действующими снип и санпин"
    assert ocr.fix_homoglyphs("Осциллограф RIGOL DHO804 или эквивалент") == "Осциллограф RIGOL DHO804 или эквивалент"
    assert ocr.fix_homoglyphs("Скорость нагрева 5 K/мин, BINDER MK 56") == "Скорость нагрева 5 K/мин, BINDER MK 56"
    assert ocr.fix_homoglyphs("Interface USB, Wi-Fi, Ethernet") == "Interface USB, Wi-Fi, Ethernet"


def test_mixed_alphabet_words_go_to_one_alphabet():
    assert ocr.fix_homoglyphs("ШT.") == "ШТ."
    assert ocr.fix_homoglyphs("Осциллограф RIGОL DНО804 или эквивалент") == "Осциллограф RIGOL DHO804 или эквивалент"
    assert ocr.fix_homoglyphs("вместо анализатора НIОКI РW3335") == "вместо анализатора HIOKI PW3335"


def _mixed_pdf(tmp_path):
    """Текстовый PDF из образца и в конце страница без текстового слоя (как скан)."""
    pymupdf = pytest.importorskip("pymupdf")
    from pathlib import Path
    src = Path("samples/pdf/lift_src.pdf")
    if not src.exists():
        pytest.skip("нет образца PDF")
    pdf = pymupdf.open(str(src))
    pdf.new_page()
    path = tmp_path / "mixed.pdf"
    pdf.save(str(path))
    return str(path), len(pdf)


@pytest.mark.parametrize("orchestrator", ["linear", "graph"])
def test_mixed_pdf_with_skipped_scan_page_is_incomplete(monkeypatch, tmp_path, orchestrator):
    from tzcheck.pipeline import check
    monkeypatch.setenv("TZCHECK_OCR", "0")
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    path, pages = _mixed_pdf(tmp_path)
    r = check(path, use_llm=False, orchestrator=orchestrator)
    assert any(w.startswith(f"mixed.pdf: стр. {pages}: нет текстового слоя, OCR недоступен") for w in r["warnings"])
    assert r["status"] == "incomplete" and r["completeness"]["incomplete_steps"] == ["parse"]
    parse_step = next(s for s in r["steps_status"] if s["id"] == "parse")
    assert parse_step["state"] == "не выполнен" and "OCR недоступен" in parse_step["reason"]
    assert r["verdict"].startswith("Проверка неполная")


def test_mixed_pdf_incomplete_with_model(scripted, monkeypatch, tmp_path):
    from tzcheck.pipeline import check
    for k in ("TZCHECK_ORCHESTRATOR", "TZCHECK_RAG", "TZCHECK_CUSTOMER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TZCHECK_OCR", "0")
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    path, _ = _mixed_pdf(tmp_path)
    r = check(path, use_llm=True)
    assert r["status"] == "incomplete" and r["completeness"]["incomplete_steps"] == ["parse"]
    assert r["summary"].startswith("Проверка неполная") and "OCR недоступен" in r["summary"]
    assert check("samples/pdf/lift_src.pdf", use_llm=False)["status"] == "ok"
