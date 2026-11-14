"""Сбой выгрузки в Word (файл правок, файл с комментариями) не теряет результат проверки: предупреждение в отчёте,
report.json, html и история сохраняются."""
import json

import docx

from tzcheck import history, jobs, report, revisions


def test_word_export_failure_keeps_result_and_history(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("docx сломан")

    monkeypatch.setattr(revisions, "write", boom)
    monkeypatch.setattr(report, "to_docx_comments", boom)
    monkeypatch.setenv("TZCHECK_REFDATA_OFFLINE", "1")
    d = docx.Document()
    d.add_paragraph("Срок поставки: в разумный срок.")
    src = tmp_path / "tz.docx"
    d.save(src)
    rid, folder = jobs.new_run(str(tmp_path / "store"))
    res = jobs.execute_check(str(tmp_path / "store"), rid, folder, [str(src)], False, None, [])
    assert res["status"] in ("ok", "incomplete") and res["revisions"] == {}
    notes = [w for w in res["warnings"] if w.startswith("Выгрузка ")]
    assert any("правок Word" in w and "docx сломан" in w for w in notes)
    assert any("комментариев Word" in w for w in notes)
    saved = json.load(open(f"{folder}/tz.report.json", encoding="utf-8"))
    assert [w for w in saved["warnings"] if w.startswith("Выгрузка ")] == notes
    assert "правок Word" in open(f"{folder}/tz.report.html", encoding="utf-8").read()
    meta = history.read_meta(folder)
    assert meta["status"] == res["status"] and meta["report"] == "tz.report.json" and meta.get("finished_at")
