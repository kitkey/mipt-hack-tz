"""Нормативная база для окна «шестерёнка»: документы корпуса норм, внутренние стандарты заказчика, загрузка и удаление.

Загруженный DOCX/PDF разбирается тем же парсером, что ТЗ, сохраняется как документ корпуса с пометкой заказчика
в <data>/norm_custom (TZCHECK_NORMS_CUSTOM_DIR) и попадает в индекс поиска норм (BM25, tzcheck.rag).
Наружу ничего не отправляется."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from . import customer, rag
from .norms import NORMS
from .parse import ParseError, parse

UPLOAD_EXT = (".docx", ".pdf", ".doc", ".rtf", ".odt", ".txt")
INTERNAL_STATUS = "внутренний стандарт заказчика"
_LOCK = threading.Lock()


def custom_dir() -> Path:
    return rag.custom_dir()


def _kind(path: Path) -> str:
    folder = path.parent.resolve()
    if folder == custom_dir().resolve():
        return "internal"
    if folder == rag.texts_dir().resolve():
        return "edition"
    return "corpus"


def _doc_id(path: Path) -> str:
    name = path.name
    return name[:-8] if name.endswith(".json.gz") else name[:-5] if name.endswith(".json") else name


def _text(d: dict) -> str:
    return "\n".join(b.get("text", "") for b in d.get("blocks", []))


def _card(path: Path, d: dict) -> dict:
    kind = _kind(path)
    return {"id": _doc_id(path), "kind": kind, "document": d.get("document", ""), "title": d.get("title", ""),
            "status": d.get("status", ""), "edition": d.get("edition", ""), "source": d.get("source", ""),
            "customer": d.get("customer", ""), "uploaded_at": d.get("uploaded_at", ""), "file": d.get("file", ""),
            "blocks": len(d.get("blocks", [])), "characters": len(_text(d)), "deletable": kind == "internal"}


def _practices() -> list[dict]:
    out = []
    for p in customer.PROFILES:
        lines = [f"{k}: {v}" for k, v in (p.get("rules") or {}).items()] + list(p.get("verify_practice") or [])
        out.append({"id": f"customer-{p['id']}", "kind": "customer_practice", "document": f"Практика закупщиков: {p['name']}",
                    "title": "", "status": "правила практики заказчика (customers.json)", "edition": "", "source": p.get("source", ""),
                    "customer": p["name"], "uploaded_at": "", "file": "", "blocks": len(lines), "characters": sum(map(len, lines)),
                    "deletable": False, "_lines": lines})
    return out


def list_documents() -> dict:
    docs = [_card(p, d) for p, d in rag.iter_all_docs(rag.MAIN_CORPUS)]
    practices = [{k: v for k, v in x.items() if not k.startswith("_")} for x in _practices()]
    order = {"internal": 0, "customer_practice": 1, "corpus": 2, "edition": 3}
    items = sorted(docs + practices, key=lambda x: (order.get(x["kind"], 9), x["document"]))
    return {"documents": items,
            "counts": {k: sum(1 for x in items if x["kind"] == k) for k in order},
            "references": [{"id": k, "title": v["title"], "text": v["text"], "url": v.get("url", ""), "edition": v.get("edition", "")}
                           for k, v in NORMS.items()],
            "index": {"fresh": rag.index_fresh(), "path": str(rag.INDEX)}}


def get_document(doc_id: str) -> dict | None:
    for x in _practices():
        if x["id"] == doc_id:
            card = {k: v for k, v in x.items() if not k.startswith("_")}
            return dict(card, text="\n".join(x["_lines"]), blocks_list=[{"type": "text", "text": t} for t in x["_lines"]])
    if doc_id in NORMS:
        n = NORMS[doc_id]
        return {"id": doc_id, "kind": "reference", "document": n["title"], "status": n.get("edition", ""), "source": n.get("url", ""),
                "deletable": False, "text": n["text"], "blocks_list": [{"type": "text", "text": n["text"]}]}
    for p, d in rag.iter_all_docs(rag.MAIN_CORPUS):
        if _doc_id(p) == doc_id:
            return dict(_card(p, d), text=_text(d), blocks_list=d.get("blocks", []))
    return None


def _corpus_blocks(doc) -> tuple[list[dict], bool]:
    """Блоки разобранного документа в формате корпуса. Пункты без нумерации нумеруются (1., 1.1., …), иначе
    разбивка корпуса на пункты (rag.chunks) их не видит."""
    blocks = [b for b in doc.blocks if b.text.strip()]
    if sum(1 for b in blocks if rag.HEADING.match(b.text.strip())) >= 2:
        return [{"type": "table" if b.kind == "row" else "text", "text": b.text.strip()} for b in blocks], False
    out, h, i = [], 0, 0
    for b in blocks:
        text = " ".join(b.text.split())
        if b.kind == "row":
            if h == 0:
                h = 1
                out.append({"type": "text", "text": f"1. {doc.name}"})
            out.append({"type": "table", "text": text})
        elif b.kind == "heading":
            h, i = h + 1, 0
            out.append({"type": "text", "text": f"{h}. {text[:1].upper()}{text[1:]}"})
        else:
            if h == 0:
                h = 1
                out.append({"type": "text", "text": f"1. {doc.name}"})
            i += 1
            out.append({"type": "text", "text": f"{h}.{i}. {text[:1].upper()}{text[1:]}"})
    return out, True


def _rebuild() -> int:
    n = rag.build()
    rag._searcher.cache_clear()
    return n


def upload(src_path: str, filename: str, title: str = "", customer_name: str = "") -> dict:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in UPLOAD_EXT:
        raise ValueError(f"формат {ext or 'без расширения'} не поддерживается, нужен docx, doc, rtf, odt, pdf или txt")
    try:
        doc = parse(src_path)
    except ParseError as e:
        raise ValueError(f"документ не разобран: {e}") from e
    blocks, numbered = _corpus_blocks(doc)
    if not blocks:
        raise ValueError("в документе нет текста")
    name = " ".join((title or os.path.splitext(filename)[0]).split())[:200]
    existing = {rag.norm_document(d.get("document", "")) for _, d in rag.iter_all_docs(rag.MAIN_CORPUS)}
    if rag.norm_document(name) in existing:
        raise FileExistsError(f"документ «{name}» уже есть в нормативной базе, укажите другое название")
    with open(src_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    doc_id = "int-" + digest[:12]
    folder = custom_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / f"{doc_id}.json").exists():
        raise FileExistsError("этот файл уже загружен")
    files = folder / "files"
    files.mkdir(exist_ok=True)
    shutil.copyfile(src_path, files / f"{doc_id}{ext}")
    record = {"id": doc_id, "document": name, "title": name, "edition": "", "edition_date": None, "status": INTERNAL_STATUS,
              "source": f"загружен через интерфейс: {filename}", "retrieved": time.strftime("%Y-%m-%d"),
              "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "customer": customer_name.strip() or "заказчик",
              "kind": "internal", "file": filename, "sha256": digest, "auto_numbered": numbered, "blocks": blocks}
    with _LOCK:
        (folder / f"{doc_id}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        rows = _rebuild()
    chunks = len(rag.chunks(record))
    return dict(_card(folder / f"{doc_id}.json", record), chunks=chunks, index_rows=rows, auto_numbered=numbered)


def upload_bytes(data: bytes, filename: str, title: str = "", customer_name: str = "") -> dict:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "upload" + os.path.splitext(filename)[1].lower())
        with open(path, "wb") as fh:
            fh.write(data)
        return upload(path, filename, title, customer_name)


def delete(doc_id: str) -> bool:
    if not re.fullmatch(r"int-[0-9a-f]{12}", doc_id or ""):
        raise PermissionError("удалять можно только загруженные внутренние стандарты")
    folder = custom_dir()
    path = folder / f"{doc_id}.json"
    if not path.exists():
        return False
    with _LOCK:
        path.unlink()
        for f in (folder / "files").glob(f"{doc_id}.*"):
            f.unlink()
        _rebuild()
    return True


def original_file(doc_id: str) -> Path | None:
    if not re.fullmatch(r"int-[0-9a-f]{12}", doc_id or ""):
        return None
    return next(iter((custom_dir() / "files").glob(f"{doc_id}.*")), None)


def search(query: str, limit: int = 5, internal_only: bool = False) -> list[dict]:
    """Поиск пунктов норм по корпусу (BM25 без порога отказа, как окно поиска); internal_only: только стандарты заказчика."""
    with _LOCK:
        rag.ensure_index()
    s = rag._searcher("bm25", "")
    allowed = {i for i, r in enumerate(s.rows) if r.get("internal")} if internal_only else None
    sc = s.scores(query, allowed)
    best = sorted(sc.items(), key=lambda x: -x[1])[:limit]
    return [dict(rag._public(s.rows[i]), score=round(v, 3), quote=rag.best_quote(s.rows[i]["text"], query)) for i, v in best]
