"""OCR сканов ТЗ: PaddleOCR PP-OCRv5, lang=ru (детектор PP-OCRv5, распознавание eslav_PP-OCRv5_mobile_rec).

Включение: TZCHECK_OCR=auto (по умолчанию: распознавать страницы без текстового слоя и картинки, если paddleocr
установлен), 1 (обязательно), 0 (выключено). TZCHECK_OCR_DEVICE=cpu|gpu, TZCHECK_OCR_DET=mobile (по умолчанию)|server.
Строки распознанного текста собираются в абзацы по геометрии рамок; строка таблицы скана остаётся одной строкой
с ячейками через « | », если рамки стоят в один ряд.
"""
from __future__ import annotations

import importlib.util
import math
import os
import re
import threading
from functools import lru_cache

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
REC_MODEL = "eslav_PP-OCRv5_mobile_rec"
DET_MODELS = {"mobile": "PP-OCRv5_mobile_det", "server": "PP-OCRv5_server_det"}
_LOCK = threading.Lock()


def mode() -> str:
    v = os.environ.get("TZCHECK_OCR", "auto").strip().lower()
    return {"1": "on", "true": "on", "yes": "on", "on": "on", "0": "off", "false": "off", "no": "off", "off": "off"}.get(v, "auto")


def available() -> bool:
    try:
        return importlib.util.find_spec("paddleocr") is not None
    except (ImportError, ValueError):
        return False


def enabled() -> bool:
    m = mode()
    return m == "on" or (m == "auto" and available())


@lru_cache(maxsize=1)
def engine():
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCR
    device = os.environ.get("TZCHECK_OCR_DEVICE", "cpu")
    det = DET_MODELS.get(os.environ.get("TZCHECK_OCR_DET", "mobile"), DET_MODELS["mobile"])
    kwargs = dict(text_detection_model_name=det, text_recognition_model_name=REC_MODEL,
                  use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False,
                  device=device)
    if device == "cpu":
        kwargs["enable_mkldnn"] = os.environ.get("TZCHECK_OCR_MKLDNN", "1") == "1"
        kwargs["cpu_threads"] = int(os.environ.get("TZCHECK_OCR_THREADS", "8"))
    return PaddleOCR(**kwargs)


LATIN_TO_CYR = str.maketrans("ABCEHKMOPTXYabcehkmnoptuxy", "АВСЕНКМОРТХУавсенкмнортиху")
CYR_TO_LATIN = str.maketrans("АВСЕНКМОРТХУаеорсух", "ABCEHKMOPTXYaeopcyx")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")


def fix_homoglyphs(line: str) -> str:
    """Распознавание eslav на 300 dpi путает короткие русские слова с латиницей («в» -> «b», «на» -> «ha», «СНиП» -> «chип»).
    Латиница заменяется кириллицей в словах со смешанным алфавитом (в том числе в короткой ячейке «ШT.»)
    и в коротких строчных словах русской строки;
    в латинском слове с буквами без кириллического двойника, наоборот, кириллица меняется на латиницу (RIGОL -> RIGOL)."""
    cyr, lat = len(_CYR.findall(line)), len(_LAT.findall(line))
    if not lat:
        return line
    russian = cyr > lat

    def fix(m):
        w = m.group(0)
        if not _LAT.search(w):
            return w
        mapped = w.translate(LATIN_TO_CYR)
        if _LAT.search(mapped):
            latin = w.translate(CYR_TO_LATIN)
            return latin if _CYR.search(w) and not _CYR.search(latin) else w
        if _CYR.search(w):
            return mapped
        if russian and (w in ("B", "C", "O", "Y") or (len(w) <= 3 and w.islower())):
            return mapped
        return w
    return _WORD.sub(fix, line)


def skew_angle(polys) -> float:
    """Наклон страницы (радианы) по верхним кромкам длинных строк: медиана, не больше 10°."""
    angles = []
    for poly in polys:
        (x0, y0), (x1, y1) = poly[0][:2], poly[1][:2]
        height = abs(float(poly[3][1]) - float(y0)) or 1.0
        if abs(x1 - x0) >= 4 * height:
            angles.append(math.atan2(float(y1) - float(y0), float(x1) - float(x0)))
    if len(angles) < 3:
        return 0.0
    angles.sort()
    a = angles[len(angles) // 2]
    return a if abs(a) <= math.radians(10) else 0.0


def deskew_transform(angle: float, width: int, height: int) -> tuple:
    """Поворот на -angle с переносом в положительные координаты: (cos, sin, tx, ty, новая ширина, новая высота)."""
    c, s_ = math.cos(angle), math.sin(angle)
    corners = ((0, 0), (width, 0), (0, height), (width, height))
    xs = [x * c + y * s_ for x, y in corners]
    ys = [-x * s_ + y * c for x, y in corners]
    return c, s_, -min(xs), -min(ys), int(max(xs) - min(xs)) + 1, int(max(ys) - min(ys)) + 1


def _boxes(result, tf=None) -> list[tuple[float, float, float, float, str, float]]:
    """Рамки строк в системе координат выровненной страницы: поворот скана снимается до группировки в строки."""
    texts = result["rec_texts"]
    scores = result.get("rec_scores", [1.0] * len(texts))
    polys = result.get("rec_polys")
    boxes = result.get("rec_boxes")
    have_polys = polys is not None and len(polys) == len(texts)
    if tf is None:
        angle = skew_angle(polys) if have_polys else 0.0
        tf = (math.cos(angle), math.sin(angle), 0.0, 0.0)
    cos, sin, tx, ty = tf[:4]
    out = []
    for i, t in enumerate(texts):
        if not t or not t.strip():
            continue
        if have_polys:
            pts = [(float(x) * cos + float(y) * sin + tx, -float(x) * sin + float(y) * cos + ty) for x, y in (p[:2] for p in polys[i])]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        else:
            x0, y0, x1, y1 = [float(v) for v in boxes[i][:4]]
        out.append((x0, y0, x1, y1, t.strip(), float(scores[i])))
    return out


def line_records(boxes) -> list[tuple[str, float, float, float, float]]:
    """Рамки -> строки по порядку чтения (текст, x0, y0, x1, y1): группировка по пересечению по вертикали,
    внутри строки слева направо. Большой разрыв по горизонтали внутри строки превращается в « | »."""
    if not boxes:
        return []
    heights = sorted(b[3] - b[1] for b in boxes)
    h = max(1.0, heights[len(heights) // 2])
    rows: list[list] = []
    for b in sorted(boxes, key=lambda b: ((b[1] + b[3]) / 2, b[0])):
        cy = (b[1] + b[3]) / 2
        if rows and abs(cy - rows[-1][0]) <= 0.55 * h:
            rows[-1][1].append(b)
            rows[-1][0] = (rows[-1][0] * (len(rows[-1][1]) - 1) + cy) / len(rows[-1][1])
        else:
            rows.append([cy, [b]])
    out = []
    for _, items in rows:
        items.sort(key=lambda b: b[0])
        parts = [items[0][4]]
        for prev, cur in zip(items, items[1:]):
            gap = cur[0] - prev[2]
            parts.append((" | " if gap > 2.5 * h else " ") + cur[4])
        out.append((fix_homoglyphs("".join(parts)), min(b[0] for b in items), min(b[1] for b in items),
                    max(b[2] for b in items), max(b[3] for b in items)))
    return out


def lines_from_boxes(boxes) -> list[str]:
    return [r[0] for r in line_records(boxes)]


def table_grid(img) -> list[list[tuple[int, int, int, int]]]:
    """Таблицы скана по линиям сетки: для каждой таблицы список ячеек (x0, y0, x1, y1) в пикселях."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 10)
    height, width = bw.shape
    hor = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, width // 40), 1)))
    ver = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, height // 80))))
    grid = cv2.dilate(hor | ver, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(grid)
    tables = []
    for i in range(1, n):
        x, y, w, h = (int(v) for v in stats[i][:4])
        if w < width * 0.3 or h < 30:
            continue
        inv = 255 - grid[y:y + h, x:x + w]
        cn, _, cs, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
        cells = []
        for j in range(1, cn):
            cx, cy, cw, ch = (int(v) for v in cs[j][:4])
            if cw < 12 or ch < 10 or cx == 0 or cy == 0 or cx + cw >= w or cy + ch >= h:
                continue
            cells.append((x + cx, y + cy, x + cx + cw, y + cy + ch))
        if len(cells) >= 2:
            tables.append(cells)
    return tables


def table_rows(cells, boxes) -> tuple[list[list[str]], set]:
    """Ячейки -> строки таблицы; объединённая по вертикали ячейка повторяется в каждой строке, как в разборе docx.
    Строки внутри ячейки разделены переводом строки: склейку переносов и разорванных кодов делает разбор PDF."""
    used, text_of = set(), {}
    for c in cells:
        inside = [k for k, b in enumerate(boxes) if c[0] <= (b[0] + b[2]) / 2 <= c[2] and c[1] <= (b[1] + b[3]) / 2 <= c[3]]
        used.update(inside)
        text_of[c] = chr(10).join(r[0] for r in line_records([boxes[k] for k in inside]))
    tops = []
    for c in sorted(cells, key=lambda c: c[1]):
        if not tops or c[1] - tops[-1] > 8:
            tops.append(c[1])
    rows = []
    for k, top in enumerate(tops):
        bottom = tops[k + 1] if k + 1 < len(tops) else max(c[3] for c in cells)
        mid = (top + bottom) / 2
        row = sorted((c for c in cells if c[1] <= mid <= c[3]), key=lambda c: c[0])
        texts = [text_of[c] for c in row]
        if any(texts):
            rows.append(texts)
    return rows, used


def _layout_here(img) -> tuple[list, list]:
    """Распознавание страницы с разметкой: строки вне таблиц (текст, x0, y0, x1, y1) и таблицы {y, rows}
    в пикселях выровненной страницы."""
    res = list(engine().predict(img))
    if not res:
        return [], []
    r = res[0]
    polys = r.get("rec_polys")
    angle = skew_angle(polys) if polys is not None and len(polys) else 0.0
    height, width = img.shape[:2]
    tf = deskew_transform(angle, width, height)
    boxes = _boxes(r, tf)
    import numpy as np
    m = np.float32([[tf[0], tf[1], tf[2]], [-tf[1], tf[0], tf[3]]])
    try:
        import cv2
        flat = cv2.warpAffine(img, m, (tf[4], tf[5]), borderValue=(255, 255, 255)) if abs(angle) > math.radians(0.2) else img
        grids = table_grid(flat)
    except Exception:
        grids = []
    tables, used = [], set()
    for cells in grids:
        rows, inside = table_rows(cells, boxes)
        if len(rows) >= 2:
            tables.append({"y": min(c[1] for c in cells), "rows": rows})
            used |= inside
    lines = line_records([b for k, b in enumerate(boxes) if k not in used])
    return lines, tables


def _lines_here(img) -> list[str]:
    res = list(engine().predict(img))
    lines = []
    for r in res:
        lines.extend(lines_from_boxes(_boxes(r)))
    return lines


_POOL = None
RESTARTS = 0


def _pool():
    global _POOL
    if _POOL is None:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        _POOL = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    return _POOL


def _in_worker(fn, img):
    global _POOL, RESTARTS
    with _LOCK:
        if os.environ.get("TZCHECK_OCR_ISOLATE", "1") != "1":
            return fn(img)
        from concurrent.futures.process import BrokenProcessPool
        for _ in range(2):
            try:
                return _pool().submit(fn, img).result()
            except BrokenProcessPool:
                RESTARTS += 1
                _POOL = None
        raise RuntimeError("процесс OCR дважды упал на этой странице")


def ocr_array(img) -> list[str]:
    """Строки текста с картинки (numpy HxWx3 BGR или путь к файлу).

    Paddle на CPU с MKLDNN изредка падает с segfault на очередной странице; по умолчанию распознавание идёт
    в отдельном процессе (TZCHECK_OCR_ISOLATE=1), который после падения перезапускается, а страница повторяется."""
    return _in_worker(_lines_here, img)


def _page_image(page, dpi):
    import numpy as np
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3][:, :, ::-1].copy()


def ocr_pdf_layout(page, dpi: int | None = None) -> tuple[list, list]:
    """Скан страницы PDF -> (строки вне таблиц в формате разбора текстового PDF, таблицы {y, rows}) в пунктах PDF."""
    dpi = dpi or int(os.environ.get("TZCHECK_OCR_DPI", "200"))
    lines, tables = _in_worker(_layout_here, _page_image(page, dpi))
    k = 72.0 / dpi
    lines = [(t, x0 * k, y0 * k, x1 * k, y1 * k, False) for t, x0, y0, x1, y1 in lines]
    return lines, [dict(t, y=t["y"] * k) for t in tables]


def ocr_pdf_page(page, dpi: int | None = None) -> list[str]:
    return ocr_array(_page_image(page, dpi or int(os.environ.get("TZCHECK_OCR_DPI", "200"))))


def ocr_image_file(path: str) -> list[list[str]]:
    """Страницы многостраничного TIFF или одна картинка -> строки по страницам."""
    import numpy as np
    from PIL import Image, ImageSequence
    pages = []
    with Image.open(path) as im:
        for frame in ImageSequence.Iterator(im):
            arr = np.asarray(frame.convert("RGB"))
            pages.append(ocr_array(arr[:, :, ::-1].copy()))
    return pages
