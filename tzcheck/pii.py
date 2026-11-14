import re

PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("PHONE", re.compile(r"(?<![\d.])(?:\+7|8)[\s(-]*\d(?:[\s()-]{0,2}\d){9}(?![\d.])")),
    ("SNILS", re.compile(r"\b\d{3}-\d{3}-\d{3}[\s-]\d{2}\b")),
    ("PASSPORT", re.compile(r"\b\d{2}\s?\d{2}\s?№?\s?\d{6}\b")),
    ("PERSON", re.compile(r"\b[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ий|ой|ая|ова|ева|ина|ына|ский|ская|ко|ук|юк|ич)\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")),
    ("PERSON", re.compile(r"\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ий|ой|ая|ова|ева|ина|ына|ский|ская|ко|ук|юк|ич)\b")),
    ("PERSON", re.compile(r"\b[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ова|ева|ина|ына|ский|ская)\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|ич)\b")),
    ("PERSON", re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична)\b")),
    ("PERSON", re.compile(r"\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]{2,}\b")),
]


class Scrubber:
    def __init__(self):
        self.mapping: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def _token(self, kind: str, value: str) -> str:
        for tok, val in self.mapping.items():
            if val == value:
                return tok
        self.counts[kind] = self.counts.get(kind, 0) + 1
        tok = f"[{kind}_{self.counts[kind]}]"
        self.mapping[tok] = value
        return tok

    def scrub(self, text: str) -> str:
        for kind, rx in PATTERNS:
            text = rx.sub(lambda m: self._token(kind, m.group(0)), text)
        return text

    def restore(self, text: str) -> str:
        for tok, val in self.mapping.items():
            text = text.replace(tok, val)
        return text

    def summary(self) -> dict:
        return dict(self.counts)
