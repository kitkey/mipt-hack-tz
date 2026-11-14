"""Длинный текст для модели без потерь: деление на куски по границам и выдержка вокруг нужных слов.

Срез по числу символов выбрасывал хвост, который модель должна была увидеть. split делит текст по переносам строк, затем по знакам
конца фразы, затем по пробелам; куски по порядку дают весь текст. window для коротких выдержек (кандидаты поиска, второе место)
показывает место вокруг слов запроса, а не начало текста. Оба только готовят вход модели, решений о дефектах здесь нет."""

SEPARATORS = ("\n", ". ", "; ", ": ", " ")
CUT = "…"


def _pieces(text: str, sep: str) -> list[str]:
    parts = text.split(sep)
    return [p + sep for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def split(text: str, limit: int) -> list[str]:
    """Куски не длиннее limit; "".join(куски) == text. Границы по убыванию: перенос строки, «. », «; », «: », пробел;
    слово длиннее предела делится по символам."""
    text = text or ""
    limit = max(1, limit)
    if len(text) <= limit:
        return [text] if text else []
    for sep in SEPARATORS:
        pieces = _pieces(text, sep)
        if len(pieces) < 2:
            continue
        out, cur = [], ""
        for p in pieces:
            if len(p) > limit:
                if cur:
                    out.append(cur)
                    cur = ""
                out.extend(split(p, limit))
            elif len(cur) + len(p) > limit:
                out.append(cur)
                cur = p
            else:
                cur += p
        if cur:
            out.append(cur)
        return _merge(out, limit)
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _merge(parts: list[str], limit: int) -> list[str]:
    """Соседние короткие куски склеиваются, пока помещаются в предел: меньше вызовов модели при том же тексте."""
    out = []
    for p in parts:
        if out and len(out[-1]) + len(p) <= limit:
            out[-1] += p
        else:
            out.append(p)
    return out


def lines(line: str, limit: int) -> list[str]:
    """Строка вида «[id] текст» длиннее предела: куски с тем же [id] в начале каждого, чтобы цитата из любого куска
    привязалась к своему блоку."""
    if len(line) <= limit:
        return [line]
    head, sep, body = line.partition("] ")
    if not sep or not head.startswith("["):
        return split(line, limit)
    head += sep
    return [head + p.strip() for p in split(body, max(200, limit - len(head))) if p.strip()]


def clip(text: str, limit: int) -> str:
    """Начало текста до limit символов по границе слова, срез помечен «…»: для подписей и пересказов, не для текста ТЗ."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", limit // 2, limit)
    return text[:cut if cut > 0 else limit].rstrip(" ,;:") + CUT


def window(text: str, query: str, limit: int) -> str:
    """Выдержка не длиннее limit вокруг слов запроса; текст короче предела идёт целиком. Сначала ищется запрос дословно
    (цитата), затем место, где слов запроса больше всего. Срезы помечены «…»."""
    text = text or ""
    if len(text) <= limit:
        return text
    start = None
    q = " ".join((query or "").split())
    if q:
        i = text.lower().find(q.lower())
        if i >= 0:
            start = i - max(0, (limit - len(q)) // 2)
    if start is None:
        from .rag import WORD, lemma, tokens
        want = {w for w in tokens(q) if len(w) > 3 or w.isdigit()}
        marks = [m.start() for m in WORD.finditer(text) if lemma(m.group(0).lower().replace("ё", "е")) in want] if want else []
        best, start = 0, 0
        j = 0
        for i, pos in enumerate(marks):
            while marks[j] < pos - limit * 0.8:
                j += 1
            if i - j + 1 > best:
                best, start = i - j + 1, marks[j] - limit // 10
    start = max(0, min(start, len(text) - limit))
    end = start + limit
    if start > 0:
        sp = text.find(" ", start, start + 40)
        start = sp + 1 if sp >= 0 else start
    if end < len(text):
        sp = text.rfind(" ", max(start, end - 40), end)
        end = sp if sp > start else end
    return (CUT if start > 0 else "") + text[start:end].strip() + (CUT if end < len(text) else "")
