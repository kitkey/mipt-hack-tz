import logging
import os
import re
import threading
from urllib.parse import urlparse

log = logging.getLogger(__name__)

PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("PHONE", re.compile(r"(?<![\d.])(?:\+7|8)[\s(-]*\d(?:[\s()-]{0,2}\d){9}(?![\d.])")),
    ("SNILS", re.compile(r"\b\d{3}-\d{3}-\d{3}[\s-]\d{2}\b")),
    # номер паспорта только после слова «паспорт» или «серия»: без контекста 10 цифр подряд это ИНН организации или номер извещения
    ("PASSPORT", re.compile(r"(?i:паспорт\w*|серия)[^\d\n]{0,25}?(\d{2}\s?\d{2}\s?(?:№\s?|номер\s?)?\d{6})(?!\d)")),
    # счёт физлица (40817, 40820) и вклад (423); расчётные счета организаций (40702 и др.) не трогаются
    ("ACCOUNT", re.compile(r"(?<!\d)(?:4081[7]|40820|423\d\d)\d{15}(?!\d)")),
    ("PERSON", re.compile(r"\b[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ий|ой|ая|ова|ева|ина|ына|ский|ская|ко|ук|юк|ич)\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")),
    ("PERSON", re.compile(r"\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ий|ой|ая|ова|ева|ина|ына|ский|ская|ко|ук|юк|ич)\b")),
    ("PERSON", re.compile(r"\b[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ова|ева|ина|ына|ский|ская)\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|ич)\b")),
    ("PERSON", re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична)\b")),
    ("PERSON", re.compile(r"\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]{2,}\b")),
]

NER_MODEL = "redmadrobot-rnd/rubert-base-pii-ner"
NER_KINDS = {
    "FIRST_NAME": "PERSON", "LAST_NAME": "PERSON", "MIDDLE_NAME": "PERSON",
    "EMAIL": "EMAIL", "PHONE": "PHONE",
    "PASSPORT": "PASSPORT", "SNILS": "SNILS", "INN": "INN", "OMS": "OMS", "CREDIT_CARD": "CARD",
    "DRIVER_LICENSE": "DOC", "MILITARY_ID": "DOC", "BIRTH_CERTIFICATE": "DOC",
}
NER_DIGITS = {"PASSPORT": (10, 10), "SNILS": (11, 11), "INN": (12, 12), "OMS": (16, 16), "CARD": (16, 19), "DOC": (9, 12)}
NER_MAX_LEN, NER_STRIDE, NER_BATCH, NER_CACHE = 512, 128, 16, 8192
TOKEN_RX = re.compile(r"\[[A-Z_]+_\d+\]")
PERSON_GAP = re.compile(r"[\s.]{0,3}")
JOIN_GAP = re.compile(r"[\s()+\-.]{0,2}")
INITIALS_BEFORE = re.compile(r"(?<![А-Яа-яЁёA-Za-z0-9.])(?:[А-ЯЁA-Z]\.\s?){1,2}\s?$")
INITIALS_AFTER = re.compile(r"\s?(?:[А-ЯЁA-Z]\.\s?){1,2}")
PERSON_CONTEXT = True
NAMED_AFTER = re.compile(r"(?<!\w)(?:им\.|имени)\s*$", re.I)
DOC_CHARS = re.compile(r"[\dA-ZА-ЯЁ№\s-]+")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}

_lock = threading.Lock()
_engine = None
_failed = False
_cache: dict[str, list] = {}


def external_model() -> bool:
    """Модель внешняя: openrouter/ или api_base не на этой машине. Без api_base LiteLLM ходит в облако провайдера, кроме ollama."""
    model = os.environ.get("TZCHECK_LLM_MODEL", "")
    if not model:
        return False
    if model.startswith("openrouter/"):
        return True
    base = os.environ.get("TZCHECK_LLM_API_BASE", "").strip()
    if not base:
        return not model.startswith(("ollama/", "ollama_chat/"))
    return (urlparse(base if "//" in base else "//" + base).hostname or "").lower() not in LOCAL_HOSTS


def ner_mode() -> str:
    """1/0 как раньше; auto (по умолчанию): слой включается, когда модель проверки внешняя и текст ТЗ уходит с машины."""
    v = os.environ.get("TZCHECK_PII_NER", "auto").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return "1"
    if v in ("0", "false", "no", "off"):
        return "0"
    return "auto"


def ner_enabled() -> bool:
    mode = ner_mode()
    if mode == "auto":
        return external_model()
    return mode == "1"


def ner_available() -> bool:
    """Модель NER загружена (первый вызов грузит её); без torch, transformers или файлов модели False."""
    return _load() is not None


def _min_score() -> float:
    return float(os.environ.get("TZCHECK_PII_NER_MIN", "0.5"))


def _load():
    global _engine, _failed
    if _engine is not None or _failed:
        return _engine
    with _lock:
        if _engine is not None or _failed:
            return _engine
        try:
            import torch
            from transformers import AutoModelForTokenClassification, AutoTokenizer
            name = os.environ.get("TZCHECK_PII_NER_MODEL") or NER_MODEL
            cache = os.environ.get("TZCHECK_PII_NER_CACHE") or None
            device = os.environ.get("TZCHECK_PII_NER_DEVICE", "cpu")
            tok = AutoTokenizer.from_pretrained(name, cache_dir=cache)
            model = AutoModelForTokenClassification.from_pretrained(name, cache_dir=cache).to(device).eval()
            _engine = (torch, tok, model, device)
        except Exception as e:
            _failed = True
            log.warning("NER для персональных данных не загружен, остаются регулярки: %s", e)
    return _engine


def _predict_many(texts: list[str]) -> list[list[tuple[str, int, int, float]]]:
    """Модель по окнам 512 токенов с перекрытием 128; тексты идут пачками близкой длины."""
    torch, tok, model, device = _engine
    best: list[dict[tuple[int, int], tuple[float, str]]] = [{} for _ in texts]
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    for g in range(0, len(order), NER_BATCH):
        idx = order[g:g + NER_BATCH]
        enc = tok([texts[i] for i in idx], return_offsets_mapping=True, truncation=True, max_length=NER_MAX_LEN,
                  stride=NER_STRIDE, return_overflowing_tokens=True, padding=True, return_tensors="pt")
        offsets = enc.pop("offset_mapping").tolist()
        sample = enc.pop("overflow_to_sample_mapping", None)
        sample = sample.tolist() if sample is not None else [0] * len(offsets)
        with torch.inference_mode():
            logits = model(**{k: v.to(device) for k, v in enc.items()}).logits
        score, label = logits.float().softmax(-1).max(-1)
        score, label = score.cpu().tolist(), label.cpu().tolist()
        for w, offs in enumerate(offsets):
            seen = best[idx[sample[w]]]
            for i, (s, e) in enumerate(offs):
                if s == e:
                    continue
                item = (score[w][i], model.config.id2label[label[w][i]])
                if (s, e) not in seen or item[0] > seen[(s, e)][0]:
                    seen[(s, e)] = item
    return [_spans(t, b) for t, b in zip(texts, best)]


def _spans(text: str, best: dict) -> list[tuple[str, int, int, float]]:
    spans: list[list] = []
    for (s, e), (p, lab) in sorted(best.items()):
        if lab == "O":
            spans.append(None)
            continue
        kind = lab[2:]
        last = spans[-1] if spans else None
        if last and last[0] == kind and (lab.startswith("I-") or JOIN_GAP.fullmatch(text[last[2]:s])) and s >= last[2]:
            last[2] = e
            last[3].append(p)
        else:
            spans.append([kind, s, e, [p]])
    out = []
    for sp in spans:
        if sp is None:
            continue
        kind, s, e, ps = sp
        while s > 0 and text[s - 1].isalnum():
            s -= 1
        while e < len(text) and text[e].isalnum():
            e += 1
        out.append((kind, s, e, sum(ps) / len(ps)))
    return out


def prefetch(texts) -> None:
    """Прогоняет модель по многим фрагментам одной пачкой и кладёт ответы в кэш, scrub потом их не пересчитывает."""
    todo = list(dict.fromkeys(t for t in texts if t and t.strip() and t not in _cache))
    if not todo or _load() is None:
        return
    try:
        found = _predict_many(todo)
    except Exception as e:
        log.warning("NER не отработал на пачке фрагментов, они пойдут по одному: %s", e)
        return
    if len(_cache) + len(todo) > NER_CACHE:
        _cache.clear()
    _cache.update(zip(todo, found))


def ner_spans(text: str) -> list[tuple[str, int, int, float]]:
    """Сырые сущности модели: (тип модели, начало, конец, средняя уверенность)."""
    if not text.strip() or _load() is None:
        return []
    if text in _cache:
        return _cache[text]
    try:
        spans = _predict_many([text])[0]
    except Exception as e:
        log.warning("NER не отработал на фрагменте, остаются регулярки: %s", e)
        spans = []
    if len(_cache) >= NER_CACHE:
        _cache.clear()
    _cache[text] = spans
    return spans


def _plausible(kind: str, value: str) -> bool:
    digits = sum(c.isdigit() for c in value)
    if kind in NER_DIGITS:
        lo, hi = NER_DIGITS[kind]
        return lo <= digits <= hi and DOC_CHARS.fullmatch(value) is not None
    if kind == "PERSON":
        return digits == 0 and value[:1].isupper() and sum(c.isalpha() for c in value) >= 2
    if kind == "EMAIL":
        return "@" in value
    if kind == "PHONE":
        return 5 <= digits <= 15 and "," not in value and not re.search(r"\d\.\d", value) and any(c in value for c in "-()+")
    return True


def person_merge(spans: list[tuple[str, int, int, float]], text: str) -> list[tuple[str, int, int, float]]:
    """Приводит типы модели к меткам Scrubber, склеивает фамилию, имя и отчество в одно ФИО и добавляет к нему инициалы.

    Одиночное слово-фамилия без инициалов и без имени в ТЗ почти всегда улица, марка или название организации
    («ул. Кулакова», «Доктор Боголюбов»), поэтому при PERSON_CONTEXT такое ФИО не маскируется.
    Имя в названии организации («им. Н.И. Пирогова») тоже не маскируется.
    """
    out: list[list] = []
    for t, s, e, p in sorted(spans, key=lambda x: x[1]):
        kind = NER_KINDS.get(t)
        if kind is None or (kind == "PERSON" and not text[s:s + 1].isupper()):
            continue
        last = out[-1] if out else None
        if last and kind == "PERSON" == last[0] and s >= last[2] and PERSON_GAP.fullmatch(text[last[2]:s]):
            last[2], last[3], last[4] = e, min(last[3], p), last[4] + 1
        elif last and s < last[2]:
            continue
        else:
            out.append([kind, s, e, p, 1])
    res = []
    for kind, s, e, p, parts in out:
        if kind == "PERSON":
            before, after = INITIALS_BEFORE.search(text, max(0, s - 12), s), INITIALS_AFTER.match(text, e)
            if before:
                s = before.start()
            if after:
                e = after.start() + len(after.group().rstrip())
            if PERSON_CONTEXT and parts < 2 and not (before or after):
                continue
            if NAMED_AFTER.search(text[max(0, s - 8):s]):
                continue
        res.append((kind, s, e, p))
    return res


class Scrubber:
    def __init__(self, ner: bool | None = None):
        self.mapping: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.ner = ner_enabled() if ner is None else ner
        # один объект на пул потоков проверки: выдача меток и восстановление под замком, иначе две метки на одно значение или одна на два
        self._lock = threading.Lock()

    def _token(self, kind: str, value: str) -> str:
        with self._lock:
            for tok, val in self.mapping.items():
                if val == value:
                    return tok
            self.counts[kind] = self.counts.get(kind, 0) + 1
            tok = f"[{kind}_{self.counts[kind]}]"
            self.mapping[tok] = value
            return tok

    def _ner_scrub(self, original: str, text: str, pos: list[int]) -> str:
        """Сущности модели ищутся в исходном тексте и маскируются в тексте после регулярок,
        если их символы там целы (не попали в метку регулярки)."""
        where = {o: i for i, o in enumerate(pos) if o >= 0}
        threshold = _min_score()
        parts, cur = [], 0
        for kind, s, e, p in person_merge(ner_spans(original), original):
            value = original[s:e]
            a, b = where.get(s), where.get(e - 1)
            if p < threshold or not _plausible(kind, value) or a is None or b is None or a < cur or b - a != e - 1 - s:
                continue
            if any(pos[k] < 0 for k in range(a, b + 1)):
                continue
            parts += [text[cur:a], self._token(kind, value)]
            cur = b + 1
        return "".join(parts) + text[cur:] if parts else text

    def prefetch(self, texts) -> None:
        if self.ner:
            prefetch(texts)

    def scrub(self, text: str) -> str:
        if not self.ner:
            for kind, rx in PATTERNS:
                g = 1 if rx.groups else 0
                text = rx.sub(lambda m: m.group(0)[:m.start(g) - m.start()] + self._token(kind, m.group(g)), text)
            return text
        original, pos = text, list(range(len(text)))
        for kind, rx in PATTERNS:
            parts, kept, last = [], [], 0
            g = 1 if rx.groups else 0
            for m in rx.finditer(text):
                tok = self._token(kind, m.group(g))
                parts += [text[last:m.start(g)], tok]
                kept += pos[last:m.start(g)] + [-1] * len(tok)
                last = m.end(g)
            if parts:
                text, pos = "".join(parts) + text[last:], kept + pos[last:]
        return self._ner_scrub(original, text, pos)

    def restore(self, text: str) -> str:
        with self._lock:
            items = list(self.mapping.items())
        for tok, val in items:
            text = text.replace(tok, val)
        return text

    def summary(self) -> dict:
        return dict(self.counts)
