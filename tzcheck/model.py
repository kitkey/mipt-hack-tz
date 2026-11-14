from dataclasses import dataclass, field, asdict

STATUS_ERROR = "ошибка"
STATUS_UNCLEAR = "неясно"
STATUS_REVIEW = "ручная проверка"
STATUS_ADVICE = "рекомендация"
SECTION_MAIN = "main"
SECTION_LLM = "llm_review"
SECTION_UNCHECKED = "unchecked"
# подпись статуса на экране и в отчётах; значение в данных прежнее
STATUS_TITLES = {STATUS_REVIEW: "на проверку"}


def status_title(status: str) -> str:
    return STATUS_TITLES.get(status, status)


def from_model(source: str) -> bool:
    """Замечание найдено моделью (а не правилом, которое модель подтвердила)."""
    return source.startswith("llm") or source == "reconcile_llm"


@dataclass
class Finding:
    block_id: str
    quote: str
    category: str
    explanation: str
    fix: str
    severity: str = "medium"
    norm_ids: list[str] = field(default_factory=list)
    needs_review: bool = False
    source: str = "rule"
    confidence: float = 0.7
    location: str = ""
    id: str = ""
    status: str = ""
    also_in: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    confidence_pct: int | None = None
    confidence_basis: str = ""
    section: str = SECTION_MAIN
    rule_id: str = ""
    confirmed_by: str = ""
    group: str = ""
    # точный текст, которым заменяется цитата (правка в режиме рецензирования); пусто, если замена не детерминирована
    replacement: str = ""
    # как найдено: шаги от ответа модели до итога [{"step", "call", "item", "text"}], полные промпты в журнале прогона (trace.json)
    trace: list = field(default_factory=list)

    def finalize(self):
        from .norms import group_of
        self.group = group_of(self.category)
        if not self.status:
            if self.needs_review:
                self.status = STATUS_REVIEW
            elif self.norm_ids == ["INTERNAL"]:
                self.status = STATUS_ADVICE
            elif self.category in ("Некорректная характеристика", "Гарантийные условия") and self.severity != "high":
                self.status = STATUS_UNCLEAR
            else:
                self.status = STATUS_ERROR
        self.evidence.setdefault("найдено", self.source)
        self.evidence.setdefault("цитата найдена в тексте", True)
        self.evidence.setdefault("основание из базы норм", bool(self.norm_ids))
        return self

    def to_dict(self):
        d = asdict(self)
        for k in [k for k in d if k.startswith("_")]:
            d.pop(k)
        return d

    def key(self):
        return (self.block_id, self.category, self.quote[:40].lower())
