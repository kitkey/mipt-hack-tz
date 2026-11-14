"""Раздел llm_review в отчёте и интерфейсе подписан «На проверку»."""
from fastapi.testclient import TestClient

from tzcheck.api import app
from tzcheck.model import SECTION_LLM, Finding
from tzcheck.report import to_html


def test_review_label_in_api_and_report():
    assert TestClient(app).get("/api/meta").json()["sections"]["llm_review"] == "на проверку"
    f = Finding("p1", "в разумный срок", "Противоречие", "high", "Срок не определён.", "Указать срок.", [], True, "model:find", 0.7, "абзац 1")
    f.section = SECTION_LLM
    d = f.finalize().to_dict()
    d["id"] = "M001"
    html = to_html({"file": "a.docx", "status": "ok", "mode": "model", "findings": [d], "summary": "", "stats": {"model_block": {"count": 1}},
                    "document": {"format": "docx", "files": ["a.docx"], "blocks": 1, "fields": {}}, "warnings": []})
    assert "На проверку (1)" in html and "Замечания модели: проверить вручную" not in html
