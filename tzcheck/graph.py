"""Тот же конвейер проверки ТЗ узлами LangGraph плюс один условный переход:
замечания с низкой уверенностью модели (или без оценки) уходят на повторную проверку моделью.
Формат результата тот же, что у pipeline.check."""
import os
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import confidence, llm
from . import progress as prog
from .checks import categories_for, only_groups, parse_checks, rules_for
from .parse import ParseError, parse_many
from .pii import Scrubber
from .pipeline import _collapse_repeats, _finish, _merge, _security_scan, candidates
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
    checks: list
    rep: Any
    public: bool


def _step(state: State, name: str) -> list:
    return state.get("steps", []) + [name]


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
    rep.done("parse", blocks=len(doc.blocks), files=len(state["paths"]))
    prog.attach(doc, rep)
    doc.check_categories = categories_for(_groups(state))
    doc.public = bool(state.get("public", False))
    # внешняя модель без NER и без пометки «открытый» не вызывается, проверка идёт правилами
    return {"doc": doc, "scr": Scrubber(), "steps": _step(state, "parse"), "use_llm": state["use_llm"] and llm.gate(doc)}


def n_security(state: State) -> State:
    rep = _rep(state)
    rep.start("security")
    warns = _security_scan(state["doc"])
    state["doc"].warnings.extend(warns)
    rep.done("security", warnings=len(warns))
    return {"steps": _step(state, "security")}


def n_rules(state: State) -> State:
    rep, groups, doc = _rep(state), _groups(state), state["doc"]
    rep.start("rules")
    findings = _collapse_repeats(only_groups(run_rules(doc, rules_for(groups)), groups), doc)
    doc.candidates = only_groups(getattr(doc, "candidates", []) or [], groups)
    rep.done("rules", findings=len(findings))
    return {"findings": findings, "steps": _step(state, "rules"), "mode": "rules+llm" if state["use_llm"] else "rules"}


def n_llm_verify_rules(state: State) -> State:
    rep = _rep(state)
    state["doc"].llm_deadline = time.time() + llm.doc_budget()
    rep.start("llm_verify_rules", candidates=sum(1 for f in state["findings"] if f.needs_review))
    findings = llm.verify_rules(state["doc"], state["findings"], state["scr"])
    rep.done("llm_verify_rules", findings=len(findings))
    return {"findings": findings, "steps": _step(state, "llm_verify_rules")}


def n_llm_find(state: State) -> State:
    rep, groups = _rep(state), _groups(state)
    doc, scr, findings = state["doc"], state["scr"], state["findings"]
    rep.start("llm_find")
    found = llm.analyze(doc, findings, scr)
    if "logic" in groups:
        rep.update(stage="сверка частей документа между собой")
        found += llm.cross_check(doc, scr)
    found = only_groups(found + candidates(doc), groups)
    rep.done("llm_find", found=len(found))
    rep.start("llm_verify_new", candidates=len(found))
    kept = llm.verify_new(doc, found, scr)
    findings = _collapse_repeats(_merge(findings, kept), doc)
    rep.done("llm_verify_new", confirmed=len(kept), findings=len(findings))
    return {"findings": findings, "steps": _step(state, "llm_find") + ["llm_verify_new"]}


def n_recheck(state: State) -> State:
    rep = _rep(state)
    rep.start("llm_recheck", candidates=sum(1 for f in state["findings"] if confidence.is_low(f)))
    findings = _collapse_repeats(llm.recheck(state["doc"], state["findings"], state["scr"]), state["doc"])
    rep.done("llm_recheck", findings=len(findings))
    return {"findings": findings, "steps": _step(state, "llm_recheck")}


def n_reconcile(state: State) -> State:
    rep, groups = _rep(state), _groups(state)
    rep.skip("llm_recheck", "нет замечаний с низкой уверенностью")
    doc, findings = state["doc"], list(state["findings"])
    rep.start("reconcile")
    rec = only_groups(reconcile(doc), groups)
    findings.extend(rec)
    rep.done("reconcile", found=len(rec))
    steps = _step(state, "reconcile")
    if state["use_llm"] and "logic" in groups:
        rep.start("reconcile_llm")
        sem = reconcile_semantic(doc, state["scr"])
        findings.extend(sem)
        rep.done("reconcile_llm", found=len(sem))
        steps.append("reconcile_llm")
    return {"findings": findings, "steps": steps}


def n_finish(state: State) -> State:
    res = _finish(state["doc"], state["paths"], state["findings"], state["scr"],
                  state["use_llm"], state["mode"], state["t0"], "graph", state["steps"] + ["finish"],
                  groups=_groups(state), rep=_rep(state))
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


def check_graph(path, use_llm: bool | None = None, checks=None, progress=None, public: bool = False) -> dict:
    """checks, progress и public как у pipeline.check; без них поведение прежнее."""
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
