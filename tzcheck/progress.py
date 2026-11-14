"""Ход проверки по шагам конвейера для интерфейса (задачи /api/jobs, поток SSE).

Шаги те же, что в pipeline.check и graph.check_graph (поле steps результата), плюс finish (_finish: профили ГОСТ,
решения закупщиков, уверенность, заключение). Обработчик progress получает события-словари; сбой обработчика
проверку не ломает. Сырые рассуждения модели сюда не попадают: передаются только счётчики."""
import re
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


# состояние шага в результате (steps_status): выполнен / неприменим / не выполнен / ошибка
STATE_RU = {"done": "выполнен", "skipped": "неприменим", "not_done": "не выполнен", "error": "ошибка"}
# предупреждения, появившиеся за время шага, по которым шаг считается не выполненным или упавшим
NOT_DONE_RE = re.compile(r"бюджет\s.*исчерпан|OCR недоступен", re.I)
ERROR_RE = re.compile(r"\bупал[аио]?\b|фрагмент пропущен|не выполнен[аоы]?\b|не выполнено запросов", re.I)
LLM_STEPS = ("llm_verify_rules", "llm_find", "llm_verify_new", "llm_recheck", "reconcile_llm")


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
        self.reasons: dict[str, str] = {}
        self.doc = None
        self._w0: dict[str, int] = {}

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
        if self.doc is not None:
            self._w0[step] = len(self.doc.warnings)
        self._emit("step_started", step, details=details)

    def update(self, **details):
        if self.current:
            self._emit("step_progress", self.current, details=details)

    def judge(self, step: str) -> tuple[str, str]:
        """Состояние шага по предупреждениям, появившимся за время шага: done, not_done или error с причиной."""
        if self.doc is None:
            return "done", ""
        new = self.doc.warnings[self._w0.get(step, 0 if step == "parse" else len(self.doc.warnings)):]
        bad = [w for w in new if ERROR_RE.search(w)]
        if bad:
            return "error", "; ".join(w[:160] for w in bad[:3])
        miss = [w for w in new if NOT_DONE_RE.search(w)]
        if miss:
            return "not_done", "; ".join(w[:160] for w in miss[:3])
        return "done", ""

    def done(self, step: str, **details):
        state, reason = self.judge(step)
        self.state[step] = state
        if reason:
            self.reasons[step] = reason
        if self.current == step:
            self.current = None
        if state == "error":
            self._emit("step_failed", step, details=dict(details, ошибка=reason))
        elif state == "not_done":
            self._emit("step_incomplete", step, details=dict(details, причина=reason))
        else:
            self._emit("step_done", step, details=details)

    def skip(self, step: str, reason: str = ""):
        if self.state.get(step) != "pending":
            return  # шаг не из плана или уже выполнен
        self.state[step] = "skipped"
        if reason:
            self.reasons[step] = reason
        self._emit("step_skipped", step, details={"причина": reason} if reason else {})

    def fail(self, step: str, error: str):
        self.state[step] = "error"
        self.reasons[step] = error
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
    rep.doc = doc


def steps_status(rep: Reporter, use_llm: bool, groups=None) -> list[dict]:
    """Состояние каждого шага для результата: выполнен / неприменим / не выполнен / ошибка, с причиной.
    Шаг, который ещё идёт (finish во время заключения), оценивается по предупреждениям на этот момент."""
    out = []
    for step, st in rep.state.items():
        reason = rep.reasons.get(step, "")
        if st == "pending" and step in LLM_STEPS and not use_llm:
            # модель запрошена, но не вызвана (нет ключа или документ не допущен к внешней модели): шаг неприменим
            blocked = [w for w in (rep.doc.warnings if rep.doc is not None else []) if w.startswith("модель не вызвана")]
            st, reason = "skipped", (blocked[0] if blocked else "модель не вызвана")
        elif st in ("running", "pending"):
            st, reason = rep.judge(step) if st == "running" else ("not_done", "шаг не запускался")
        out.append({"id": step, "title": STEP_TITLES.get(step, step), "state": STATE_RU.get(st, st), "reason": reason})
    seen = {x["id"] for x in out}
    for step in LLM_STEPS:
        if step in seen or step == "llm_recheck":
            continue
        if not use_llm:
            out.append({"id": step, "title": STEP_TITLES[step], "state": STATE_RU["skipped"], "reason": "модель не подключена"})
        elif step == "reconcile_llm" and groups is not None and "logic" not in groups:
            out.append({"id": step, "title": STEP_TITLES[step], "state": STATE_RU["skipped"], "reason": "группа «Логика» не выбрана"})
    return out


def incomplete(statuses: list[dict]) -> list[dict]:
    return [x for x in statuses if x["state"] in (STATE_RU["not_done"], STATE_RU["error"])]


def tick(doc, **details):
    """Счётчики текущего шага из глубины конвейера (фрагмент i из n у модели)."""
    rep = getattr(doc, "_progress", None)
    if rep is not None:
        rep.update(**details)
