"""Модель таблиц: разметку шапки (что значит каждый столбец) даёт модель, один вызов на шапку; код раскладывает
строки по этой разметке в позиции.

Позиция: наименование, ОКПД 2, КТРУ, количество, единица, характеристики [{имя, значение, единица, block_id}].
Раскладка только по структуре: объединённая по вертикали ячейка наименования продолжает позицию, новая ячейка
наименования или номера начинает новую, вложенная таблица относится к позиции строки, в ячейке которой стоит."""
from dataclasses import dataclass, field

from .ask import ask, pmap, wrap
from .chunks import clip

ROLES = ("num", "name", "okpd", "ktru", "qty", "unit", "char_name", "value", "char_unit", "desc", "bidder", "instruction", "price", "other")
TITLES = {"num": "№", "name": "Наименование", "okpd": "ОКПД 2", "ktru": "КТРУ", "qty": "Количество", "unit": "Ед. изм.",
          "char_name": "Характеристика", "value": "Значение", "char_unit": "Ед. изм. значения", "desc": "Описание",
          "bidder": "Заполняет участник", "instruction": "Инструкция", "price": "Цена", "other": "Столбец"}
KINDS = ("positions", "characteristics", "other")

PROMPT = """Задача: разметка шапки таблицы.
Таблица из комплекта документов закупки «{subject}». Текст перед таблицей: «{caption}».
Первые строки таблицы: номер строки, затем столбцы «[индекс] текст»; «→» значит, что ячейка объединена с левой, «↓» с верхней.
{rows}

Определи:
- header_rows: сколько строк сверху занимает шапка (0, если шапки нет);
- kind: positions, если строка или группа строк описывает позицию закупки (товар, работу, услугу); characteristics, если таблица описывает
  характеристики одного товара или объекта; other, если это не позиции (график, расчёт, перечень документов, подписи, текст в рамке);
- item: для characteristics наименование товара или объекта, если оно видно из текста перед таблицей или из строк, иначе пусто;
- columns: роль каждого столбца: num (номер позиции), name (наименование позиции), okpd (код ОКПД 2), ktru (код КТРУ), qty (количество),
  unit (единица количества), char_name (наименование характеристики), value (значение характеристики), char_unit (единица значения),
  desc (характеристики или описание сплошным текстом), bidder (заполняет участник), instruction (инструкция по заполнению), price, other;
  в таблице характеристик столбец с названием характеристики или параметра это char_name, а не name; если у одной роли несколько
  столбцов (шапка объединена над ними), дай роль каждому;
  label: подпись столбца коротко, как в шапке.

Верни JSON: {{"header_rows": число, "kind": "positions|characteristics|other", "item": "...",
"columns": [{{"index": номер столбца, "role": "...", "label": "..."}}]}}"""


@dataclass
class Column:
    index: int
    label: str
    role: str


@dataclass
class Char:
    name: str
    value: str
    unit: str
    block_id: str
    group: str = ""

    def to_dict(self):
        return {"имя": self.name, "значение": self.value, "единица": self.unit, "block_id": self.block_id}


@dataclass
class Position:
    id: str
    source: str
    table: int
    name: str = ""
    num: str = ""
    okpd: str = ""
    ktru: str = ""
    qty: str = ""
    unit: str = ""
    chars: list[Char] = field(default_factory=list)
    block_ids: list[str] = field(default_factory=list)
    cells: dict = field(default_factory=dict)  # block_id -> [(подпись, роль, текст)]

    def to_dict(self):
        return {"id": self.id, "наименование": self.name, "ОКПД 2": self.okpd, "КТРУ": self.ktru, "количество": self.qty,
                "единица": self.unit, "характеристики": [c.to_dict() for c in self.chars], "блоки": self.block_ids}


@dataclass
class TableModel:
    key: tuple
    columns: list[Column]
    header_ids: list[str]
    kind: str
    caption: str = ""
    item: str = ""
    positions: list[Position] = field(default_factory=list)
    marked: bool = False

    def col(self, role: str) -> int | None:
        return next((c.index for c in self.columns if c.role == role), None)


@dataclass
class RowInfo:
    table: TableModel
    position: Position | None
    header: bool = False


class Model:
    def __init__(self):
        self.tables: list[TableModel] = []
        self.positions: list[Position] = []
        self.rows: dict[str, RowInfo] = {}

    def to_dict(self):
        return {"tables": [{"key": list(t.key[:2]), "kind": t.kind, "marked": t.marked,
                            "columns": [{"index": c.index, "role": c.role, "label": c.label} for c in t.columns]} for t in self.tables],
                "positions": [p.to_dict() for p in self.positions]}


def _grid(b) -> list[str]:
    return b.grid if b.grid else list(b.cells)


def _flat(s: str) -> str:
    return " ".join((s or "").split())


def _flag(lst, i) -> bool:
    return i < len(lst) and bool(lst[i])


def groups(doc) -> list[list]:
    out, index = {}, []
    for b in doc.blocks:
        if b.kind != "row":
            continue
        k = (b.source, b.table, b.parent, b.nested)
        if k not in out:
            out[k] = []
            index.append(k)
        out[k].append(b)
    return [out[k] for k in index]


def _caption(doc, first) -> str:
    i = next((n for n, b in enumerate(doc.blocks) if b.id == first.id), 0)
    for b in reversed(doc.blocks[max(0, i - 3):i]):
        if b.kind != "row":
            return b.text[:200]
    return ""


def _render_rows(rows, scr, limit: int = 5) -> str:
    out = []
    for n, b in enumerate(rows[:limit], 1):
        g = _grid(b)
        cells = []
        for i, t in enumerate(g):
            mark = "→" if _flag(b.hmerge, i) else "↓" if _flag(b.vmerge, i) else ""
            cells.append(f"[{i}] {mark}{scr.scrub(clip(_flat(t), 120)) if not mark else ''}")
        out.append(f"строка {n}: " + " | ".join(cells))
    return "\n".join(out)


def _signature(rows) -> tuple:
    return tuple(tuple(_flat(t) for t in _grid(b)) for b in rows[:2])


def mark(doc, rows, scr, subject: str) -> dict:
    prompt = PROMPT.format(subject=scr.scrub(subject)[:200], caption=scr.scrub(_caption(doc, rows[0]))[:200],
                           rows=wrap(_render_rows(rows, scr)))
    where = rows[0].location().rsplit(", строка", 1)[0]
    return ask(doc, prompt, 2000, "разметка шапки таблицы", "tables", f"Разметка шапки: {where}",
               {"kind": "table", "title": where, "blocks": [b.id for b in rows[:5]]}) or {}


def _columns(data: dict, width: int) -> list[Column]:
    cols = {}
    for c in data.get("columns") or []:
        if not isinstance(c, dict):
            continue
        try:
            i = int(c.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < width and i not in cols:
            role = c.get("role") if c.get("role") in ROLES else "other"
            cols[i] = Column(i, _flat(str(c.get("label") or "")), role)
    return [cols.get(i, Column(i, "", "other")) for i in range(width)]


def _single(b) -> bool:
    texts = [_flat(t) for i, t in enumerate(_grid(b)) if _flat(t) and not _flag(b.hmerge, i)]
    return len(set(texts)) == 1 and len(_grid(b)) >= 3


def _layout(model: Model, tm: TableModel, rows, header_n: int, doc):
    head = rows[:header_n]
    sigs = {tuple(_flat(t) for t in _grid(h)) for h in head}
    for h in head:
        model.rows[h.id] = RowInfo(tm, None, True)
        tm.header_ids.append(h.id)
    cur = None
    group = ""
    ni, nu = tm.col("name"), tm.col("num")
    for b in rows[header_n:]:
        g = _grid(b)
        if tuple(_flat(t) for t in g) in sigs:
            # повтор шапки на новой странице
            model.rows[b.id] = RowInfo(tm, None, True)
            continue
        if tm.kind == "other":
            model.rows[b.id] = RowInfo(tm, None)
            continue
        if _single(b):
            # строка-подзаголовок группы, растянутая на всю ширину
            group = _flat(next(t for t in g if _flat(t)))
            model.rows[b.id] = RowInfo(tm, cur)
            if cur is not None:
                cur.block_ids.append(b.id)
            continue
        if tm.kind == "positions":
            name = _flat(g[ni]) if ni is not None and ni < len(g) else ""
            num = _flat(g[nu]) if nu is not None and nu < len(g) else ""
            cont = (ni is not None and _flag(b.vmerge, ni)) or (not name and not num)
            new_num = num and nu is not None and not _flag(b.vmerge, nu) and (cur is None or num != cur.num)
            if cur is None or new_num or (not cont and name):
                cur = Position(f"P{len(model.positions) + 1}", b.source, b.table, name=name, num=num)
                model.positions.append(cur)
                tm.positions.append(cur)
        elif cur is None:
            cur = Position(f"P{len(model.positions) + 1}", b.source, b.table, name=_parent_name(model, b) or tm.item)
            model.positions.append(cur)
            tm.positions.append(cur)
        cur.block_ids.append(b.id)
        cur.cells[b.id] = [(c.label, c.role, _flat(g[c.index])) for c in tm.columns
                           if c.index < len(g) and not _flag(b.hmerge, c.index) and not _flag(b.vmerge, c.index)]
        by = {}
        for c in tm.columns:
            if c.index >= len(g) or _flag(b.hmerge, c.index):
                continue
            text, merged = _flat(g[c.index]), _flag(b.vmerge, c.index)
            if c.role not in by:
                by[c.role] = (text, merged)
            elif text and text not in by[c.role][0].split("; "):
                prev = by[c.role][0]
                by[c.role] = (f"{prev}; {text}" if prev else text, by[c.role][1] and merged)
        for role in ("okpd", "ktru", "qty", "unit", "num"):
            text, merged = by.get(role, ("", False))
            if text and not getattr(cur, role):
                setattr(cur, role, text)
        cn, cn_merged = by.get("char_name", ("", False))
        if cn and not cn_merged:
            cur.chars.append(Char(cn, by.get("value", ("", False))[0], by.get("char_unit", ("", False))[0], b.id, group))
        model.rows[b.id] = RowInfo(tm, cur)


def _parent_name(model: Model, b) -> str:
    info = model.rows.get(b.parent) if b.parent else None
    return info.position.name if info and info.position else ""


def build(doc, scr, marks: dict | None = None) -> Model:
    """marks: {(файл, таблица, родитель, номер вложенной): ответ модели}; без разметки таблица остаётся «не распознана»."""
    from .passport import checked
    model = Model()
    subject = (getattr(doc, "passport", None) or {}).get("предмет") or doc.name
    tabs = groups(doc)
    todo = [rows for rows in tabs if marks is None and len(rows) >= 2 and checked(rows[0])
            and max(len(_grid(r)) for r in rows) >= 2]
    found = dict(marks or {})
    if marks is None:
        cache: dict = {}
        sigs = {}
        for rows in todo:
            sigs.setdefault(_signature(rows), rows)
        answers = pmap(lambda rows: mark(doc, rows, scr, subject), list(sigs.values()))
        for sig, ans in zip(sigs, answers):
            cache[sig] = ans
        for rows in todo:
            found[rows[0].table, rows[0].source, rows[0].parent, rows[0].nested] = cache.get(_signature(rows)) or {}
    for rows in tabs:
        k = (rows[0].table, rows[0].source, rows[0].parent, rows[0].nested)
        data = found.get(k) or {}
        width = max(len(_grid(r)) for r in rows)
        kind = data.get("kind") if data.get("kind") in KINDS else "other"
        tm = TableModel((rows[0].source, rows[0].table, rows[0].parent, rows[0].nested), _columns(data, width), [], kind,
                        _caption(doc, rows[0]), scr.restore(str(data.get("item") or "")), marked=bool(data))
        model.tables.append(tm)
        try:
            header_n = max(0, min(int(data.get("header_rows") or 0), len(rows)))
        except (TypeError, ValueError):
            header_n = 0
        _layout(model, tm, rows, header_n, doc)
    # вложенная таблица характеристик относится к позиции внешней строки
    for tm in model.tables:
        if tm.key[2] and tm.kind == "characteristics":
            parent = model.rows.get(tm.key[2])
            if parent and parent.position:
                for p in list(tm.positions):
                    parent.position.chars.extend(p.chars)
                    parent.position.block_ids.extend(p.block_ids)
                    parent.position.cells.update(p.cells)
                    for bid in p.block_ids:
                        model.rows[bid].position = parent.position
                    model.positions.remove(p)
                tm.positions = []
    doc.tables_model = model
    return model


def of(doc) -> Model:
    m = getattr(doc, "tables_model", None)
    if m is None:
        from .pii import Scrubber
        m = build(doc, Scrubber(), marks={})
    return m


def position(doc, block_id: str) -> Position | None:
    info = of(doc).rows.get(block_id)
    return info.position if info else None


def render(doc, p: Position, scr, limit: int = 7000) -> str:
    """Позиция с подписанными столбцами: каждая строка таблицы со своим id."""
    head = [f"Позиция {p.num or p.id}: {scr.scrub(p.name) or '(наименование не указано)'}"]
    for title, val in (("ОКПД 2", p.okpd), ("КТРУ", p.ktru), ("Количество", " ".join(x for x in (p.qty, p.unit) if x))):
        if val:
            head.append(f"{title}: {scr.scrub(val)}")
    lines = ["; ".join(head)]
    blocks = {b.id: b for b in doc.blocks}
    size = len(lines[0])
    for bid in p.block_ids:
        cells = p.cells.get(bid)
        if cells:
            body = " | ".join(f"{label or TITLES.get(role, role)}: {scr.scrub(text) if text else '(пусто)'}" for label, role, text in cells
                              if text or role in ("value", "char_name", "qty", "unit"))
        else:
            body = scr.scrub(blocks[bid].text) if bid in blocks else ""
        if not body:
            continue
        lines.append(f"[{bid}] {body}")
        size += len(lines[-1])
        if size > limit:
            lines.append("…")
            break
    return "\n".join(lines)
