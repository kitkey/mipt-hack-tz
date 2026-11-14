"""Общая заглушка модели для тестов интерфейса и отчётов: ядро проверки отвечает по началу задачи в запросе.

Поиск дефектов: на каждую строку [id] проверяемой части длиной от 6 слов замечание с цитатой из первых слов строки,
категория по очереди из разрешённых в запросе; вторая проверка подтверждает всё."""
import hashlib
import json
import re
import sys
from types import SimpleNamespace

import pytest

from tzcheck import llm


def _allowed(prompt: str) -> list[str]:
    part = prompt.split("по группам:\n", 1)[-1].split("\n\n", 1)[0]
    cats = []
    for line in part.splitlines():
        if ":" in line:
            cats += [c.strip() for c in line.split(":", 1)[1].split(";") if c.strip()]
    return cats


class ScriptedModel:
    def __init__(self, per_unit: int = 3, verify_score=90, find_score=60, replace=True):
        self.per_unit, self.verify_score, self.find_score, self.replace = per_unit, verify_score, find_score, replace
        self.prompts = []

    def asked(self, key: str) -> list[str]:
        return [p for p in self.prompts if p.startswith("Задача: " + key)]

    def __call__(self, prompt, max_tokens=4000):
        self.prompts.append(prompt)
        if prompt.startswith("Задача: паспорт"):
            return {"passport": {"law": "223-ФЗ", "method": "запрос котировок", "competitive": True, "kind": "товар",
                                 "subject": "поставка", "customer": "Заказчик", "customer_profile": ""}, "roles": []}
        if prompt.startswith("Задача: разметка шапки"):
            return {"header_rows": 0, "kind": "other", "item": "", "columns": []}
        if prompt.startswith("Задача: найти дефекты"):
            cats = _allowed(prompt)
            data = prompt.split("<<<ТЗ>>>", 1)[-1]
            out = []
            for bid, text in re.findall(r"^\[([\w]+)\] (?:\([^)]*\) )?(.+)$", data, re.M):
                words = text.split()
                if len(words) < 6 or not cats:
                    continue
                n = int(hashlib.md5(bid.encode()).hexdigest(), 16)
                quote = " ".join(words[:5])
                cat = cats[n % len(cats)]
                out.append({"block_id": bid, "quote": quote, "category": cat, "severity": ("high", "medium", "low")[n % 3],
                            "explanation": "Формулировка допускает разное толкование.", "fix": "Уточнить формулировку.",
                            "replacement": " ".join(words[:4]) + " УТОЧНЕНО" if self.replace and n % 4 == 0 else "",
                            "norm_ids": [], "needs_review": False, "confidence": self.find_score})
                if len(out) >= self.per_unit:
                    break
            return {"findings": out, "lookups": []}
        if prompt.startswith("Задача: независимая вторая проверка"):
            ns = re.findall(r"^(\d+)\. \[", prompt.split("<<<ТЗ>>>", 1)[-1], re.M)
            return {"decisions": [{"n": int(n), "decision": "keep", "reason": "по тексту", "confidence": self.verify_score} for n in ns]}
        if "Верни JSON: {\"summary\"" in prompt:
            return {"summary": "Заключение заглушки."}
        return {"findings": [], "decisions": []}


def fake_litellm(answer):
    """Модуль-заглушка litellm: llm._call идёт целиком (журнал, разбор JSON), ответ даёт answer(prompt) -> dict."""
    def completion(**kw):
        prompt = kw["messages"][-1]["content"]
        text = json.dumps(answer(prompt), ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=text))],
                               usage=SimpleNamespace(prompt_tokens=len(prompt) // 4, completion_tokens=len(text) // 4))
    return SimpleNamespace(completion=completion)


@pytest.fixture
def scripted(monkeypatch):
    s = ScriptedModel()
    monkeypatch.setenv("TZCHECK_LLM_MODEL", "stub/model")
    monkeypatch.setenv("TZCHECK_LLM_WORKERS", "1")
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm(s))
    return s
