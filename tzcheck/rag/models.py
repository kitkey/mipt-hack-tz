"""Эмбеддинги пунктов норм, гибрид с BM25 через RRF и реранкер bge-reranker-v2-m3.

Сборка векторов: python -m tzcheck.rag.models build --model deepvk/USER-bge-m3 [--device cuda]
"""
import argparse
import hashlib
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path

from . import INDEX, CORPUS, Search, _public, mentioned_documents, explicit_unknown

MODELS = ("deepvk/USER-bge-m3", "BAAI/bge-m3", "ai-forever/FRIDA")
RERANKER = "BAAI/bge-reranker-v2-m3"
# Выбор по замеру eval/rag/run_eval.py на 177 вопросах (eval/rag/REPORT.md).
DEFAULT_MODE = "hybrid"
DEFAULT_MODEL = "deepvk/USER-bge-m3"
# Пороги отказа открытого поиска по eval/rag/metrics_v2.json (максимум сбалансированной точности на 421 вопросе).
# Открытый поиск по умолчанию выключен (TZCHECK_RAG_OPEN=1): оценка лучшего фрагмента почти не отделяет вопросы без ответа.
REFUSE = {"bm25": 42.7, "dense": 0.70, "hybrid": 0.66, "rerank": 0.68}
MAX_SEQ = int(os.environ.get("TZCHECK_RAG_MAX_SEQ", "512"))


def vector_dir() -> Path:
    return Path(os.environ.get("TZCHECK_RAG_VECTOR_DIR", CORPUS / "vectors"))


def model_key(model: str) -> str:
    return model.replace("/", "--")


def fingerprint(index=None) -> str:
    return hashlib.sha256(Path(index or INDEX).read_bytes()).hexdigest()


def prefix(model: str, query: bool = False) -> str:
    if "FRIDA" in model:
        return "search_query: " if query else "search_document: "
    if "e5" in model.lower():
        return "query: " if query else "passage: "
    return ""


def doc_text(row: dict) -> str:
    return f"{row['document']} {row['number']} {row.get('title', '')}\n{row['text'][:1800]}"


def _device() -> str:
    return os.environ.get("TZCHECK_RAG_DEVICE", "cpu")


def encoder(model: str, device: str | None = None):
    from sentence_transformers import SentenceTransformer
    import torch
    torch.set_num_threads(int(os.environ.get("TZCHECK_RAG_CPU_THREADS", "4")))
    x = SentenceTransformer(model, device=device or _device())
    x.max_seq_length = MAX_SEQ
    return x


def cross_encoder(device: str | None = None):
    from sentence_transformers import CrossEncoder
    return CrossEncoder(RERANKER, device=device or _device(), max_length=MAX_SEQ)


def build(model: str, device: str | None = None, batch: int | None = None, duty: float | None = None) -> int:
    """Векторы всех фрагментов индекса. На GPU нагрузка ограничивается паузами между пачками (duty)."""
    import numpy as np
    s = Search(INDEX)
    out = vector_dir()
    out.mkdir(parents=True, exist_ok=True)
    device = device or _device()
    e = encoder(model, device)
    batch = batch or int(os.environ.get("TZCHECK_RAG_BATCH", "16" if device != "cpu" else "8"))
    duty = duty if duty is not None else float(os.environ.get("TZCHECK_RAG_GPU_DUTY", "0.6"))
    texts = [prefix(model) + doc_text(r) for r in s.rows]
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    vecs = np.zeros((len(texts), e.get_sentence_embedding_dimension()), dtype="float32")
    t_start = time.time()
    for k in range(0, len(order), batch):
        ids = order[k:k + batch]
        t = time.time()
        vecs[ids] = e.encode([texts[i] for i in ids], batch_size=batch, normalize_embeddings=True, show_progress_bar=False)
        if device != "cpu" and 0 < duty < 1:
            time.sleep((time.time() - t) * (1 / duty - 1))
        if (k // batch) % 100 == 0:
            print(model, k + len(ids), "/", len(order), f"{time.time() - t_start:.0f} c", flush=True)
    name = out / model_key(model)
    np.save(str(name) + ".npy", vecs)
    Path(str(name) + ".json").write_text(json.dumps({"fingerprint": fingerprint(), "model": model, "count": len(texts),
                                                     "max_seq_length": MAX_SEQ, "seconds": round(time.time() - t_start, 1),
                                                     "device": device}), encoding="utf8")
    print("saved", model, len(texts), f"{time.time() - t_start:.0f} c", flush=True)
    return len(texts)


def load_vectors(model: str, index=None):
    import numpy as np
    name = vector_dir() / model_key(model)
    meta = json.loads(Path(str(name) + ".json").read_text(encoding="utf8"))
    if meta["fingerprint"] != fingerprint(index):
        raise ValueError("Векторы собраны для другой версии индекса: пересоберите tzcheck.rag.models build")
    return np.load(str(name) + ".npy", mmap_mode="r")


def rrf(lists, k: int = 60) -> list[int]:
    points = defaultdict(float)
    for seq in lists:
        for rank, i in enumerate(seq, 1):
            points[i] += 1 / (k + rank)
    return sorted(points, key=lambda i: -points[i])


class HybridSearch:
    """mode: dense (только эмбеддинги), hybrid (RRF эмбеддингов и BM25), rerank (hybrid + кросс-энкодер)."""

    def __init__(self, model: str = DEFAULT_MODEL, mode: str = DEFAULT_MODE, rerank: bool = False, index=None):
        import numpy as np
        self.np = np
        self.mode = "rerank" if rerank else mode
        self.lex = Search(index or INDEX)
        self.rows = self.lex.rows
        self.model_name = model
        self.vectors = load_vectors(model, index)
        self.encoder = encoder(model)
        self.cross = cross_encoder() if self.mode == "rerank" else None
        self.refuse_threshold = REFUSE.get(self.mode, 0.5)
        self.name = self.mode

    def dense_scores(self, query: str):
        vec = self.encoder.encode([prefix(self.model_name, True) + query], normalize_embeddings=True, show_progress_bar=False)[0]
        return self.vectors @ vec

    def ranked(self, query: str, allowed=None, depth: int = 100):
        """Возвращает (порядок индексов, оценка порога по каждому индексу)."""
        np = self.np
        dense = self.dense_scores(query)
        if allowed is not None:
            mask = np.zeros(len(self.rows), dtype=bool)
            mask[list(allowed)] = True
            dense = np.where(mask, dense, -1.0)
        d_order = [int(i) for i in np.argsort(-dense)[:depth] if dense[int(i)] > -1]
        gate = {i: float(dense[i]) for i in d_order}
        if self.mode == "dense":
            return d_order, gate
        lex = self.lex.scores(query, allowed)
        l_order = sorted(lex, key=lambda i: -lex[i])[:depth]
        order = rrf([d_order, l_order])
        for i in order:
            gate.setdefault(i, float(dense[i]))
        if self.cross is not None and order:
            pool = order[:20]
            values = self.cross.predict([(query, doc_text(self.rows[i])) for i in pool], batch_size=4, show_progress_bar=False,
                                        activation_fn=None)
            prob = {i: 1 / (1 + math.exp(-float(v))) for i, v in zip(pool, values)}
            gate.update(prob)
            order = sorted(pool, key=lambda i: -prob[i]) + order[20:]
        return order, gate

    def search(self, query, limit=10, threshold=None, documents=None, articles=None):
        if documents is None and articles is None:
            docs = mentioned_documents(query, self.rows)
            if docs:
                documents = docs
            elif explicit_unknown(query, self.rows):
                return []
        allowed = self.lex.allowed(documents, articles)
        if allowed is not None and not allowed:
            return []
        threshold = self.refuse_threshold if threshold is None else threshold
        order, gate = self.ranked(query, allowed)
        if not order or gate.get(order[0], -1) < threshold:
            return []
        return [dict(_public(self.rows[i]), score=round(gate.get(i, 0.0), 4)) for i in order[:limit]]


def main():
    p = argparse.ArgumentParser(prog="python -m tzcheck.rag.models")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--model", default=DEFAULT_MODEL)
    b.add_argument("--device", default=None)
    b.add_argument("--batch", type=int, default=None)
    q = sub.add_parser("search")
    q.add_argument("query")
    q.add_argument("--mode", default=DEFAULT_MODE, choices=["dense", "hybrid", "rerank"])
    q.add_argument("--model", default=DEFAULT_MODEL)
    q.add_argument("--limit", type=int, default=5)
    a = p.parse_args()
    if a.cmd == "build":
        build(a.model, a.device, a.batch)
    else:
        hits = HybridSearch(a.model, a.mode).search(a.query, limit=a.limit, threshold=-1)
        print(json.dumps([{k: h[k] for k in ("document", "number", "title", "score")} | {"text": h["text"][:300]} for h in hits],
                         ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
