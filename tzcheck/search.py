"""Поиск кандидатов по комплекту документов: BM25 по леммам (тот же, что у базы норм, tzcheck.rag.Search).
Поиск только подбирает места; указано ли там искомое и противоречат ли они друг другу, решает модель."""
from .rag import Search


class DocIndex:
    def __init__(self, doc):
        self.blocks = list(doc.blocks)
        rows = [{"document": b.id, "number": "", "title": "", "text": f"{b.section} {b.text}"} for b in self.blocks]
        self.search = Search(rows=rows)

    def top(self, query: str, k: int = 6, exclude=(), only=None, min_match: int = 1) -> list:
        """Блоки комплекта по запросу: exclude — id, которые не брать; only(block) — фильтр; min_match=0 для запроса
        из коротких слов (сокращение из трёх букв само совпадением не считается)."""
        exclude = set(exclude)
        sc = self.search.scores(query, min_match=min_match)
        out = []
        for i, s in sorted(sc.items(), key=lambda x: -x[1]):
            b = self.blocks[i]
            if b.id in exclude or (only is not None and not only(b)):
                continue
            out.append((b, round(s, 2)))
            if len(out) >= k:
                break
        return out


def of(doc) -> DocIndex:
    idx = getattr(doc, "_search_index", None)
    if idx is None:
        idx = doc._search_index = DocIndex(doc)
    return idx
