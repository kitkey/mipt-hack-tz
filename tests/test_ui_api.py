"""Бэкенд интерфейса проверки: группы, выбор проверок, ход по шагам, правки в режиме рецензирования,
сопоставление требований, нормативная база, история."""
import copy
import io
import json
import os
import re
import time

import docx
from docx.oxml import OxmlElement
import pytest

from tzcheck import feedback, history, llm, rag, replacements, req_compare, revisions
from tzcheck.norms import CATEGORIES, CATEGORY_GROUP, CHECK_GROUPS, categories_for, parse_checks
from tzcheck.parse import parse
from tzcheck.pipeline import check

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
LIFT, LIFT_FIN = os.path.join(SAMPLES, "lift_src.docx"), os.path.join(SAMPLES, "lift_fin.docx")
METRO = os.path.join(SAMPLES, "metro_src.docx")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "PATH", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    monkeypatch.delenv("TZCHECK_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("TZCHECK_RAG", raising=False)
    monkeypatch.delenv("TZCHECK_LLM_MODEL", raising=False)


def test_every_category_has_group():
    assert set(CATEGORY_GROUP) == set(CATEGORIES)
    assert set(CATEGORY_GROUP.values()) == set(CHECK_GROUPS.values())


def test_findings_have_group_and_group_stats(scripted):
    r = check(LIFT, use_llm=True)
    main = [f for f in r["findings"] if f["section"] == "main"]
    assert all(f["group"] == CATEGORY_GROUP[f["category"]] for f in r["findings"])
    assert sum(r["stats"]["by_group"].values()) == len(main)
    assert set(r["stats"]["groups"]) == {"Логика", "Стандарты", "Риски"}
    assert "По группам:" in r["summary"]
    assert r["checks"] == list(CHECK_GROUPS) and r["full_check"] is True
    assert r["verdict"] and r["summary"].startswith(r["verdict"])


# ---------- 2. выбор проверок ----------

def test_parse_checks():
    assert parse_checks(None) == ["logic", "standards", "risks"]
    assert parse_checks("risks,logic") == ["logic", "risks"]
    assert parse_checks(["standards", "risks"]) == ["standards", "risks"]
    assert parse_checks("all") == ["logic", "standards", "risks"]
    with pytest.raises(ValueError):
        parse_checks("design")


def test_default_checks_change_nothing(scripted):
    a, b = check(LIFT, use_llm=True), check(LIFT, use_llm=True, checks=["logic", "standards", "risks"])
    key = lambda r: [(f["id"], f["category"], f["quote"]) for f in r["findings"]]
    assert key(a) == key(b)


@pytest.mark.parametrize("keys", [["logic"], ["standards"], ["risks"], ["logic", "risks"]])
def test_partial_check_keeps_only_selected_groups(scripted, keys):
    part = check(METRO, use_llm=True, checks=keys)
    names = {CHECK_GROUPS[k] for k in keys}
    assert part["findings"] and {f["group"] for f in part["findings"]} <= names
    assert part["full_check"] is False and part["checks"] == keys
    assert part["summary"].startswith("Проверка неполная") and "Вывод о готовности ТЗ к публикации" in part["verdict"]
    assert part["verdict_code"] != "clean"


def test_partial_check_without_findings_is_not_publishable(tmp_path):
    p = tmp_path / "plain.txt"
    p.write_text("Техническое задание\nПоставка бумаги для офиса в количестве 10 пачек.\n", encoding="utf-8")
    r = check(str(p), use_llm=False, checks=["risks"])
    assert r["verdict_code"] in ("partial", "review", "fix", "blocked", "review_high")
    if not [f for f in r["findings"] if f["section"] == "main"]:
        assert r["verdict_code"] == "partial"


def _offered(prompt: str) -> list[str]:
    part = prompt.split("по группам:\n", 1)[1].split("\n\n", 1)[0]
    return [c.strip() for line in part.splitlines() for c in line.split(":", 1)[1].split(";")]


def test_prompt_categories_limited_to_selected_groups(scripted):
    check(LIFT, use_llm=True, checks=["risks"])
    find = scripted.asked("найти дефекты")
    assert find and all(sorted(_offered(p)) == sorted(categories_for(["risks"])) for p in find)
    # сверка частей даёт только «Противоречие» и «Предмет закупки»: без логики не вызывается
    assert not scripted.asked("сверка частей")


def test_full_prompt_categories_unchanged(scripted):
    check(LIFT, use_llm=True)
    assert sorted(_offered(scripted.asked("найти дефекты")[0])) == sorted(CATEGORIES)


# ---------- 3. ход по шагам ----------

def _steps(events):
    order, state = [], {}
    for e in events:
        if e["event"] == "plan":
            order = [s["id"] for s in e["steps"]]
        elif e["event"] in ("step_done", "step_skipped"):
            state[e["step"]] = e["event"]
    return order, state


def test_progress_rules_only():
    ev = []
    r = check(LIFT, use_llm=False, progress=ev.append)
    order, state = _steps(ev)
    assert order == r["steps"] + ["finish"]
    assert all(state[s] == "step_done" for s in order)
    parse_done = next(e for e in ev if e["event"] == "step_done" and e["step"] == "parse")
    assert parse_done["details"]["blocks"] == r["document"]["blocks"] and parse_done["title"] == "Разбор документа"


def test_progress_with_model(scripted):
    ev = []
    r = check(LIFT, use_llm=True, progress=ev.append)
    order, state = _steps(ev)
    assert order == r["steps"] + ["finish"]
    assert all(state[s] == "step_done" for s in order)
    stages = [e["details"].get("stage") for e in ev if e["event"] == "step_progress" and e["step"] == "core_find"]
    assert any("не указано" in (x or "") for x in stages)
    assert all(set(e.get("details", {})) <= {"blocks", "files", "warnings", "findings", "candidates", "found", "confirmed", "stage", "law",
                                              "customer", "tables", "positions", "model", "unchecked"} for e in ev if "details" in e)


def test_progress_graph_with_model(scripted):
    ev = []
    r = check(LIFT, use_llm=True, orchestrator="graph", progress=ev.append)
    order, state = _steps(ev)
    assert order == r["steps"] and [s for s in order if state.get(s) == "step_done"] == r["steps"]


def test_check_without_progress_still_works():
    r = check(LIFT, use_llm=False, orchestrator="graph")
    assert r["status"] == "ok" and r["steps"][-1] == "finish"


# ---------- 4. правки в режиме рецензирования ----------

def test_replacement_comes_from_model_and_only_in_main(scripted):
    r = check(LIFT, use_llm=True)
    with_rep = [f for f in r["findings"] if f["replacement"]]
    assert with_rep and all(f["replacement"].endswith(" УТОЧНЕНО") and f["section"] == "main" for f in with_rep)
    scripted.replace = False
    r = check(LIFT, use_llm=True)
    assert not any(f["replacement"] for f in r["findings"])


def test_add_equivalent_replacement():
    from tzcheck.model import Finding
    f = Finding("p1", "кнопки ЛифтПро BW-1 с подсветкой", "Ограничение конкуренции",
                "Указана конкретная марка или модель (BW-1) без слов «или эквивалент»: это прямо запрещено.", "x",
                rule_id="brand.no_equiv_high", source="rule:brand")
    assert replacements.suggest(f) == "кнопки ЛифтПро BW-1 (или эквивалент) с подсветкой"


def _accept(path) -> str:
    """Текст документа при принятии всех правок: w:del убрать, w:ins оставить."""
    d = docx.Document(path)
    for el in list(d.element.body.iter(W + "del")):
        el.getparent().remove(el)
    for ins in list(d.element.body.iter(W + "ins")):
        for r in list(ins):
            ins.addprevious(r)
        ins.getparent().remove(ins)
    return "\n".join(p.text for p in d.paragraphs)


def _reject(path) -> str:
    d = docx.Document(path)
    for el in list(d.element.body.iter(W + "ins")):
        el.getparent().remove(el)
    for dl in list(d.element.body.iter(W + "del")):
        for t in list(dl.iter(W + "delText")):
            new = OxmlElement("w:t")
            new.text = t.text
            t.addprevious(new)
            t.getparent().remove(t)
        for r in list(dl):
            dl.addprevious(r)
        dl.getparent().remove(dl)
    return "\n".join(p.text for p in d.paragraphs)


def test_revisions_docx(tmp_path, scripted):
    r = check(LIFT, use_llm=True)
    out = revisions.write(r, LIFT, str(tmp_path))
    main = out["main"]
    assert main["path"] and main["path"].endswith("lift_src.revisions.docx")
    expected = {f["id"] for f in r["findings"] if f["replacement"]}
    # цитата, которая в DOCX разорвана полем или стоит в ячейке с переносами, пропускается с причиной
    assert main["applied"] and set(main["applied"]) | set(main["skipped"]) == expected
    d = docx.Document(main["path"])
    ins = list(d.element.body.iter(W + "ins"))
    dels = list(d.element.body.iter(W + "del"))
    assert ins and dels and all(e.get(W + "author") == "Проверка ТЗ" for e in ins + dels)
    assert len(list(d.comments)) == len(main["applied"])
    accepted, rejected = _accept(main["path"]), _reject(main["path"])
    for f in r["findings"]:
        if f["id"] in main["applied"]:
            assert f["replacement"] in accepted
    original = "\n".join(p.text for p in docx.Document(LIFT).paragraphs)
    assert rejected == original


def test_revisions_skip_when_nothing_to_change(tmp_path, scripted):
    r = check(LIFT, use_llm=True)
    for f in r["findings"]:
        f["replacement"] = ""
    assert revisions.write(r, LIFT, str(tmp_path)) == {}
    assert not (tmp_path / "lift_src.revisions.docx").exists()


# ---------- 5. сопоставление требований ----------

def test_requirements_pairing_lift():
    req = req_compare.compare_requirements(parse(LIFT), parse(LIFT_FIN))
    c = req["counts"]
    assert c["unchanged"] > 100 and c["changed"] >= 5 and c["added"] >= 1
    pairs = {(x["old"]["text"], x["new"]["text"]) for x in req["changed"]}
    assert any("Тип лифта" in o and "пассажирский" in o and "грузовой" in n for o, n in pairs)
    assert any("Грузоподъемность" in o and "300" in o and "500" in n for o, n in pairs)
    assert any("Грузовой лифт" in a["text"] for a in req["added"])
    changed = next(x for x in req["changed"] if "Тип лифта" in x["old"]["text"])
    assert changed["diff"] == [{"op": "заменено", "было": "пассажирский", "стало": "грузовой"}]


def test_requirements_pairing_ignores_renumbering():
    a = req_compare.Req("old", "t1r2", "позиция", "", "", "", "", "")
    old = req_compare.extract(parse(METRO), "old")
    same = copy.deepcopy(old)
    req = req_compare.pair(old, same)
    assert req["counts"]["unchanged"] == len(old) and not req["changed"] and a.public()["kind"] == "позиция"


def test_requirements_summary_marks_repeats(scripted):
    old, new = check(LIFT, use_llm=True), check(LIFT_FIN, use_llm=True)
    req = req_compare.compare_requirements(old["_doc"], new["_doc"])
    s = req_compare.summarize(old, new, req)
    assert s["repeated"] and all(x["old_finding_id"] for x in s["repeated"])
    total = len(s["repeated"]) + len(s["on_changed"]) + len(s["on_added"]) + s["other_count"]
    assert total == len(new["findings"])
    assert "ошибка повторяется" in s["summary"]


# ---------- API: задачи, SSE, история, сопоставление, нормы ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from tzcheck import api
    monkeypatch.setattr(api, "STORE", str(tmp_path / "runs"))
    os.makedirs(api.STORE, exist_ok=True)
    return TestClient(api.app)


def _wait(client, job_id, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.2)
    raise AssertionError("задача не завершилась")


def _upload(path):
    return (os.path.basename(path), open(path, "rb"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def test_job_flow_sse_and_history(client, scripted):
    r = client.post("/api/jobs", data={"use_llm": "1", "user": "Иванова", "checks": "standards"}, files={"file": _upload(LIFT)})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    j = _wait(client, job_id)
    assert j["status"] == "done", j
    ids = [s["id"] for s in j["steps"]]
    assert ids == ["parse", "security", "passport", "tables", "core_find", "core_verify", "finish", "report"]
    assert all(s["status"] == "done" and s["started_at"] and s["finished_at"] for s in j["steps"])
    assert "блоков разобрано" in j["steps"][0]["text"]
    res = j["result"]
    assert res["checks"] == ["standards"] and {f["group"] for f in res["findings"]} == {"Стандарты"}
    assert res["links"]["revisions_docx"].endswith("lift_src.revisions.docx")
    assert client.get(res["links"]["revisions_docx"]).status_code == 200
    body = client.get(f"/api/jobs/{job_id}/events").text
    events = re.findall(r"^event: (\w+)$", body, re.M)
    assert events[0] == "snapshot" and "plan" in events and "step" in events and events[-1] == "done"
    runs = client.get("/api/runs").json()
    card = runs["items"][0]
    assert card["run_id"] == job_id and card["user"] == "Иванова" and card["check_groups"] == ["Стандарты"] and card["number"] == 1
    assert card["counts"]["by_group"] == {"Стандарты": res["stats"]["by_group"]["Стандарты"]} and card["full_check"] is False
    fid = res["findings"][0]["id"]
    assert client.post("/api/feedback", json={"run_id": job_id, "finding_id": fid, "decision": "accept"}).status_code == 200
    full = client.get(f"/api/runs/{job_id}").json()
    assert full["run"]["decisions"]["принято"] == 1 and full["decisions"][fid]["decision"] == "accept"
    assert full["findings"] == res["findings"]


def test_history_picks_up_old_runs(client, scripted):
    from tzcheck import api
    old = os.path.join(api.STORE, "0123456789ab")
    os.makedirs(old)
    r = check(LIFT, use_llm=True)
    data = {k: v for k, v in r.items() if not k.startswith("_") and k not in ("checks", "check_groups", "full_check", "verdict", "verdict_code")}
    for f in data["findings"]:
        f.pop("group")
        f.pop("replacement")
    json.dump(data, open(os.path.join(old, "lift_src.report.json"), "w", encoding="utf-8"), ensure_ascii=False)
    os.makedirs(os.path.join(api.STORE, "fedcba987654", "old"))  # прежнее /compare без отчёта: в историю не идёт
    items = client.get("/api/runs").json()["items"]
    assert [c["run_id"] for c in items] == ["0123456789ab"]
    c = items[0]
    assert c["user"] == "" and c["full_check"] and c["verdict_code"] == "blocked" and c["verdict"].startswith("Публиковать без исправлений нельзя")
    assert sum(c["counts"]["by_group"].values()) == c["counts"]["total"] > 0
    assert client.get("/api/runs/0123456789ab").json()["file"] == "lift_src.docx"
    assert client.get("/api/runs/nope").status_code == 404


def test_old_api_check_still_works(client, scripted):
    r = client.post("/api/check?use_llm=true", files={"file": _upload(LIFT)})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["findings"] and j["run_id"] and "summary" in j and "stats" in j
    r = client.post("/check", data={"use_llm": "1", "checks": ["risks"], "user": "Петров"}, files={"file": _upload(LIFT)})
    assert r.status_code == 200 and "Проверка неполная" in r.text and "<b>Риски:" in r.text
    assert "lift_src.revisions.docx" in r.text and "предлагаемыми правками" in r.text


def test_requirements_compare_api(client, scripted):
    r = client.post("/api/requirements_compare", data={"user": "Иванова", "use_llm": "1"}, files={"old": _upload(LIFT), "new": _upload(LIFT_FIN)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["requirements"]["counts"]["changed"] >= 5 and j["findings_summary"]["repeated"]
    assert j["result"]["file"] == "lift_fin.docx" and j["old_file"] == "lift_src.docx"
    card = client.get("/api/runs").json()["items"][0]
    assert card["kind"] == "requirements_compare" and card["previous_file"] == "lift_src.docx"
    assert client.get(f"/api/runs/{j['run_id']}").json()["requirements_compare"]["requirements"]["counts"] == j["requirements"]["counts"]


def test_job_requirements_compare(client):
    r = client.post("/api/jobs?use_llm=false", files={"file": _upload(LIFT_FIN), "previous": _upload(LIFT)})
    j = _wait(client, r.json()["job_id"])
    assert j["status"] == "done" and j["kind"] == "requirements_compare"
    assert [s["id"] for s in j["steps"]][-3:] == ["report", "check_old", "req_pairing"]
    assert j["result"]["requirements_compare"]["requirements"]["counts"]["changed"] >= 5


def test_job_bad_input(client):
    assert client.post("/api/jobs", data={"checks": "design"}, files={"file": _upload(LIFT)}).status_code == 400
    assert client.get("/api/jobs/doesnotexist").status_code == 404


# ---------- нормативная база ----------

@pytest.fixture
def corpus(tmp_path, monkeypatch):
    base = tmp_path / "corpus"
    base.mkdir()
    doc = {"id": "law-x", "document": "223-ФЗ", "edition": "", "status": "действует", "source": "тест",
           "blocks": [{"type": "text", "text": "Статья 3. Принципы и основные положения закупки товаров, работ, услуг"},
                      {"type": "text", "text": "1. При закупке товаров, работ, услуг заказчики руководствуются принципами информационной открытости закупки."}]}
    (base / "law-x.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rag, "CORPUS", base)
    monkeypatch.setattr(rag, "MAIN_CORPUS", base)
    monkeypatch.setattr(rag, "INDEX", base / "index.json")
    monkeypatch.setattr(rag, "texts_dir", lambda: tmp_path / "no_texts")
    monkeypatch.setenv("TZCHECK_NORMS_CUSTOM_DIR", str(tmp_path / "custom"))
    rag._searcher.cache_clear()
    rag.build()
    yield base
    rag._searcher.cache_clear()


def _standard_docx() -> bytes:
    d = docx.Document()
    d.add_heading("Стандарт организации о закупках лабораторной посуды", 1)
    d.add_paragraph("Колбы мерные поставляются с сертификатом калибровки, выданным аккредитованной лабораторией.")
    d.add_paragraph("Флюгегехаймер ультразвуковой калибруется поставщиком не реже одного раза в квартал за свой счёт.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_norms_upload_search_delete(client, corpus):
    q = "флюгегехаймер ультразвуковой калибруется раз в квартал"
    assert client.get("/api/norms/search", params={"q": q}).json()["items"] == []
    r = client.post("/api/norms", data={"title": "СТО Лаборатория 1-2026", "customer": "МФТИ"},
                    files={"file": ("sto.docx", _standard_docx(), "application/octet-stream")})
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["kind"] == "internal" and up["customer"] == "МФТИ" and up["chunks"] >= 2 and up["deletable"]
    hits = client.get("/api/norms/search", params={"q": q}).json()["items"]
    assert hits and hits[0]["document"] == "СТО Лаборатория 1-2026" and hits[0]["customer"] == "МФТИ" and hits[0]["internal"]
    assert "Флюгегехаймер" in hits[0]["text"]
    only = client.get("/api/norms/search", params={"q": "принципами информационной открытости закупки", "internal_only": True}).json()["items"]
    assert only == []
    lst = client.get("/api/norms").json()
    kinds = {x["id"]: x["kind"] for x in lst["documents"]}
    assert kinds[up["id"]] == "internal" and kinds["law-x"] == "corpus" and lst["counts"]["internal"] == 1
    assert any(x["kind"] == "customer_practice" for x in lst["documents"]) and lst["references"]
    doc = client.get(f"/api/norms/{up['id']}").json()
    assert "Флюгегехаймер ультразвуковой" in doc["text"]
    assert client.get(f"/api/norms/{up['id']}/file").status_code == 200
    assert client.post("/api/norms", data={"title": "СТО Лаборатория 1-2026"},
                       files={"file": ("sto2.docx", _standard_docx(), "application/octet-stream")}).status_code == 409
    assert client.delete("/api/norms/law-x").status_code == 403
    assert client.delete(f"/api/norms/{up['id']}").status_code == 200
    assert client.get("/api/norms/search", params={"q": q}).json()["items"] == []
    assert client.get(f"/api/norms/{up['id']}").status_code == 404


def test_meta(client):
    m = client.get("/api/meta").json()
    assert [g["key"] for g in m["groups"]] == ["logic", "standards", "risks"]
    assert sum(len(g["categories"]) for g in m["groups"]) == len(CATEGORIES) and m["steps"]["core_find"]
