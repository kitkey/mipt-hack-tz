"""Тот же конвейер проверки ТЗ узлами LangGraph плюс один условный переход:
замечания с низкой уверенностью модели (или без оценки) уходят на повторную проверку моделью.
Формат результата тот же, что у pipeline.check."""
import os
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import confidence, llm
from .parse import ParseError, parse_many
from .pii import Scrubber
from .pipeline import _collapse_repeats, _finish, _merge, _security_scan
from .reconcile import reconcile, reconcile_semantic
from .rules import run_rules


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


def _step(state: State, name: str) -> list:
    return state.get("steps", []) + [name]


def n_parse(state: State) -> State:
    try:
        doc = parse_many(state["paths"])
    except ParseError as e:
        p = state["paths"][0]
        return {"result": {"file": os.path.basename(os.fspath(p)), "status": "error", "error": str(e), "findings": [],
                           "summary": f"Документ не проверен: {e}"}, "steps": _step(state, "parse")}
    return {"doc": doc, "scr": Scrubber(), "steps": _step(state, "parse")}


def n_security(state: State) -> State:
    state["doc"].warnings.extend(_security_scan(state["doc"]))
    return {"steps": _step(state, "security")}


def n_rules(state: State) -> State:
    return {"findings": _collapse_repeats(run_rules(state["doc"]), state["doc"]), "steps": _step(state, "rules"),
            "mode": "rules+llm" if state["use_llm"] else "rules"}


def n_llm_verify_rules(state: State) -> State:
    state["doc"].llm_deadline = time.time() + llm.doc_budget()
    return {"findings": llm.verify_rules(state["doc"], state["findings"], state["scr"]), "steps": _step(state, "llm_verify_rules")}


def n_llm_find(state: State) -> State:
    doc, scr, findings = state["doc"], state["scr"], state["findings"]
    found = llm.analyze(doc, findings, scr) + llm.cross_check(doc, scr)
    findings = _collapse_repeats(_merge(findings, llm.verify_new(doc, found, scr)), doc)
    return {"findings": findings, "steps": _step(state, "llm_find") + ["llm_verify_new"]}


def n_recheck(state: State) -> State:
    findings = _collapse_repeats(llm.recheck(state["doc"], state["findings"], state["scr"]), state["doc"])
    return {"findings": findings, "steps": _step(state, "llm_recheck")}


def n_reconcile(state: State) -> State:
    doc, findings = state["doc"], list(state["findings"])
    findings.extend(reconcile(doc))
    steps = _step(state, "reconcile")
    if state["use_llm"]:
        findings.extend(reconcile_semantic(doc, state["scr"]))
        steps.append("reconcile_llm")
    return {"findings": findings, "steps": steps}


def n_finish(state: State) -> State:
    res = _finish(state["doc"], state["paths"], state["findings"], state["scr"],
                  state["use_llm"], state["mode"], state["t0"], "graph", state["steps"] + ["finish"])
    return {"result": res}


def after_parse(state: State) -> str:
    return "end" if state.get("result") else "security"


def after_rules(state: State) -> str:
    return "llm_verify_rules" if state["use_llm"] else "reconcile"


def after_find(state: State) -> str:
    """Условный переход: есть замечание с уверенностью ниже TZCHECK_CONF_LOW или без оценки → повторная проверка."""
    return "llm_recheck" if any(confidence.is_low(f) for f in state["findings"]) else "reconcile"


def build():
    g = StateGraph(State)
    for name, fn in [("parse", n_parse), ("security", n_security), ("rules", n_rules), ("llm_verify_rules", n_llm_verify_rules),
                     ("llm_find", n_llm_find), ("llm_recheck", n_recheck), ("reconcile", n_reconcile), ("finish", n_finish)]:
        g.add_node(name, fn)
    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", after_parse, {"security": "security", "end": END})
    g.add_edge("security", "rules")
    g.add_conditional_edges("rules", after_rules, {"llm_verify_rules": "llm_verify_rules", "reconcile": "reconcile"})
    g.add_edge("llm_verify_rules", "llm_find")
    g.add_conditional_edges("llm_find", after_find, {"llm_recheck": "llm_recheck", "reconcile": "reconcile"})
    g.add_edge("llm_recheck", "reconcile")
    g.add_edge("reconcile", "finish")
    g.add_edge("finish", END)
    return g.compile()


_GRAPH = None


def graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build()
    return _GRAPH


def check_graph(path, use_llm: bool | None = None) -> dict:
    paths = [path] if isinstance(path, (str, os.PathLike)) else list(path)
    if use_llm is None:
        use_llm = llm.enabled()
    out = graph().invoke({"paths": paths, "use_llm": bool(use_llm), "t0": time.time(), "steps": []})
    return out["result"]


def mermaid() -> str:
    return graph().get_graph().draw_mermaid()
