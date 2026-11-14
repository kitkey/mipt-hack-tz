"""Ход проверки по шагам конвейера для интерфейса (задачи /api/jobs, поток SSE).

Шаги те же, что в pipeline.check и graph.check_graph (поле steps результата), плюс finish (_finish: профили ГОСТ,
решения закупщиков, уверенность, заключение). Обработчик progress получает события-словари; сбой обработчика
проверку не ломает. Сырые рассуждения модели сюда не попадают: передаются только счётчики."""
import time

STEP_TITLES = {
    "parse": "Разбор документа",
    "security": "Поиск скрытых символов и инструкций для ИИ",
    "rules": "Проверка правилами",
    "llm_verify_rules": "Модель перепроверяет спорные замечания правил",
    "llm_find": "Модель ищет замечания по фрагментам документа",
    "llm_verify_new": "Независимая перепроверка находок модели",
    "llm_recheck": "Повторная проверка замечаний с низкой уверенностью",
    "reconcile": "Сверка требований между разделами",
    "reconcile_llm": "Модель сверяет похожие требования",
    "finish": "Итог: профили ГОСТ, решения закупщиков, уверенность, заключение",
    # шаги задачи вокруг конвейера (tzcheck.jobs)
    "check_old": "Проверка прошлого документа",
    "req_pairing": "Сопоставление требований двух документов",
    "report": "Выгрузка отчёта, файла с комментариями и файла с правками",
}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def plan_steps(orchestrator: str, use_llm: bool, groups=None) -> list[str]:
    """Шаги, которые конвейер выполнит при этих настройках; llm_recheck у графа условный и может быть пропущен."""
    logic = groups is None or "logic" in groups
    steps = ["parse", "security", "rules"]
    if use_llm:
        steps += ["llm_verify_rules", "llm_find", "llm_verify_new"] + (["llm_recheck"] if orchestrator == "graph" else [])
    steps.append("reconcile")
    if use_llm and logic:
        steps.append("reconcile_llm")
    return steps + ["finish"]


class Reporter:
    """Пишет события шагов в callback(event: dict). Без callback ничего не делает."""

    def __init__(self, callback=None, prefix: str = ""):
        self.callback = callback
        self.prefix = prefix
        self.current = None
        self.state: dict[str, str] = {}

    def _emit(self, event: str, step: str | None = None, **data):
        if self.callback is None:
            return
        ev = {"event": event, "ts": now_iso()}
        if step:
            ev["step"] = self.prefix + step
            ev["title"] = STEP_TITLES.get(step, step)
        ev.update(data)
        try:
            self.callback(ev)
        except Exception:
            pass

    def plan(self, steps: list[str]):
        for s in steps:
            self.state.setdefault(s, "pending")
        self._emit("plan", steps=[{"id": self.prefix + s, "title": STEP_TITLES.get(s, s)} for s in steps])

    def start(self, step: str, **details):
        self.current = step
        self.state[step] = "running"
        self._emit("step_started", step, details=details)

    def update(self, **details):
        if self.current:
            self._emit("step_progress", self.current, details=details)

    def done(self, step: str, **details):
        self.state[step] = "done"
        if self.current == step:
            self.current = None
        self._emit("step_done", step, details=details)

    def skip(self, step: str, reason: str = ""):
        if self.state.get(step) != "pending":
            return  # шаг не из плана или уже выполнен
        self.state[step] = "skipped"
        self._emit("step_skipped", step, details={"причина": reason} if reason else {})

    def fail(self, step: str, error: str):
        self.state[step] = "error"
        self.current = None
        self._emit("step_failed", step, details={"ошибка": error})

    def close(self):
        """Шаги из плана, до которых конвейер не дошёл (условный переход графа), помечаются пропущенными."""
        for s, st in list(self.state.items()):
            if st == "pending":
                self.skip(s, "условие перехода не выполнено")


def reporter(callback=None, prefix: str = "") -> Reporter:
    return callback if isinstance(callback, Reporter) else Reporter(callback, prefix)


def attach(doc, rep: Reporter):
    doc._progress = rep


def tick(doc, **details):
    """Счётчики текущего шага из глубины конвейера (фрагмент i из n у модели)."""
    rep = getattr(doc, "_progress", None)
    if rep is not None:
        rep.update(**details)
