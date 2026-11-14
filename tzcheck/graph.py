"""Тот же конвейер проверки ТЗ узлами LangGraph: без модели после разбора и проверки безопасности сразу итог,
с моделью паспорт и разметка таблиц, поиск дефектов, сверка частей, вторая проверка. Формат результата тот же, что у pipeline.check."""
import os
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import llm
from . import progress as prog
from .checks import categories_for, parse_checks
from .parse import ParseError, parse_many
from .pii import Scrubber
from .pipeline import _finish, _security_scan, core_cross, core_find, core_passport, core_verify, model_allowed, start_core


class State(TypedDict, total=False):
    paths: list
    use_llm: bool
    t0: float
    doc: Any
    scr: Any
    findings: list
    mode: str
    steps: list
    result: dict
    checks: list
    rep: Any
    public: bool


def _step(state: State, *names: str) -> list:
    return state.get("steps", []) + list(names)


def _rep(state: State):
    return state.get("rep") or prog.reporter(None)


def _groups(state: State) -> list:
    return parse_checks(state.get("checks"))


def n_parse(state: State) -> State:
    rep = _rep(state)
    rep.start("parse")
    try:
        doc = parse_many(state["paths"])
    except ParseError as e:
        rep.fail("parse", str(e))
        p = state["paths"][0]
        return {"result": {"file": os.path.basename(os.fspath(p)), "status": "error", "error": str(e), "findings": [],
                           "summary": f"Документ не проверен: {e}"}, "steps": _step(state, "parse")}
    prog.attach(doc, rep)  # до итога разбора: пропущенные страницы делают проверку неполной
    rep.done("parse", blocks=len(doc.blocks), files=len(state["paths"]))
    doc.check_categories = categories_for(_groups(state))
    doc.public = bool(state.get("public", False))
    # внешняя модель без NER и без пометки «открытый» не вызывается: только оформление и безопасность
    use_llm = model_allowed(doc, state["use_llm"])
    return {"doc": doc, "scr": Scrubber(), "steps": _step(state, "parse"), "findings": [], "mode": "model" if use_llm else "format",
            "use_llm": use_llm}


def n_security(state: State) -> State:
    rep = _rep(state)
    rep.start("security")
    warns = _security_scan(state["doc"])
    state["doc"].warnings.extend(warns)
    rep.done("security", warnings=len(warns))
    return {"steps": _step(state, "security")}


def n_passport(state: State) -> State:
    start_core(state["doc"])
    core_passport(state["doc"], state["scr"], _rep(state))
    return {"steps": _step(state, "passport", "tables")}


def n_find(state: State) -> State:
    found = core_find(state["doc"], state["scr"], _groups(state), _rep(state))
    return {"findings": found, "steps": _step(state, "core_find")}


def n_cross(state: State) -> State:
    found = core_cross(state["doc"], state["scr"], _groups(state), _rep(state))
    return {"findings": state["findings"] + found, "steps": _step(state, "core_cross")}


def n_verify(state: State) -> State:
    kept = core_verify(state["doc"], state["scr"], _groups(state), state["findings"], _rep(state))
    return {"findings": kept, "steps": _step(state, "core_verify")}


def n_finish(state: State) -> State:
    res = _finish(state["doc"], state["paths"], state["findings"], state["scr"],
                  state["use_llm"], state["mode"], state["t0"], "graph", state["steps"] + ["finish"],
                  groups=_groups(state), rep=_rep(state))
    return {"result": res}


def after_parse(state: State) -> str:
    return "end" if state.get("result") else "security"


def after_security(state: State) -> str:
    return "passport" if state["use_llm"] else "finish"


def after_find(state: State) -> str:
    return "core_cross" if "logic" in _groups(state) else "core_verify"


def build():
    g = StateGraph(State)
    for name, fn in [("parse", n_parse), ("security", n_security), ("passport", n_passport), ("core_find", n_find),
                     ("core_cross", n_cross), ("core_verify", n_verify), ("finish", n_finish)]:
        g.add_node(name, fn)
    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", after_parse, {"security": "security", "end": END})
    g.add_conditional_edges("security", after_security, {"passport": "passport", "finish": "finish"})
    g.add_edge("passport", "core_find")
    g.add_conditional_edges("core_find", after_find, {"core_cross": "core_cross", "core_verify": "core_verify"})
    g.add_edge("core_cross", "core_verify")
    g.add_edge("core_verify", "finish")
    g.add_edge("finish", END)
    return g.compile()


_GRAPH = None


def graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build()
    return _GRAPH


def check_graph(path, use_llm: bool | None = None, checks=None, progress=None, public: bool = False) -> dict:
    """checks, progress и public как у pipeline.check."""
    paths = [path] if isinstance(path, (str, os.PathLike)) else list(path)
    if use_llm is None:
        use_llm = llm.enabled()
    groups = parse_checks(checks)
    rep = prog.reporter(progress)
    rep.plan(prog.plan_steps("graph", bool(use_llm), groups))
    out = graph().invoke({"paths": paths, "use_llm": bool(use_llm), "t0": time.time(), "steps": [], "checks": groups, "rep": rep,
                          "public": bool(public)})
    return out["result"]


def mermaid() -> str:
    return graph().get_graph().draw_mermaid()
