"""Клиент текстового Jev-совместимого /v1/systemone; не подключён к конвейеру."""
from __future__ import annotations

import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tzcheck.pii import Scrubber

NONE_KEY = "__none__"


def decide(state: str, question: str, options: dict[str, str], *, base_url: str,
           model: str | None = None, api_key: str | None = None,
           timeout: float = 120.0) -> dict[str, float]:
    """Вероятности выбора, включая отдельный вариант NONE_KEY.

    Внешнему серверу передаются только предварительно разрешённые к отправке данные.
    Маскирование ПД использует существующие правила проекта и не гарантирует обнаружение всех ПД.
    """
    if not isinstance(state, str) or not isinstance(question, str) or not question.strip():
        raise ValueError("state и непустой question должны быть строками")
    if not options or NONE_KEY in options or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in options.items()):
        raise ValueError("options должны быть непустым отображением строк, без зарезервированного ключа")
    scrubber = Scrubber()
    keys = list(options) + [NONE_KEY]
    wire_keys = [f"option_{i}" for i in range(len(keys))]
    criteria = {wire_keys[i]: scrubber.scrub(options[key]) for i, key in enumerate(keys[:-1])}
    criteria[wire_keys[-1]] = "Ни один из предложенных вариантов не подходит"
    payload = {"state": scrubber.scrub(state), "questions": {"decision": {
        "type": "choice", "instructions": scrubber.scrub(question), "criteria": criteria}}}
    if model:
        payload["model"] = model
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    endpoint = base_url.rstrip("/")
    endpoint += "/systemone" if endpoint.endswith("/v1") else "/v1/systemone"
    req = Request(endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError("Ошибка запроса System One") from exc
    try:
        probabilities = data["answers"]["decision"]["probabilities"]
        if set(probabilities) != set(criteria):
            raise ValueError("неверный набор вариантов")
        result = {key: float(probabilities[wire_key]) for key, wire_key in zip(keys, wire_keys)}
        if any(not math.isfinite(p) or p < 0 or p > 1 for p in result.values()):
            raise ValueError("недопустимая вероятность")
        if not math.isclose(sum(result.values()), 1.0, abs_tol=0.03):
            raise ValueError("вероятности не суммируются в единицу")
        return result
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("Некорректный ответ System One") from exc
