FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZCHECK_STORE=/data/runs TZ=MSK-3 \
    HF_HOME=/models/hf PADDLE_PDX_CACHE_HOME=/models/paddlex TZCHECK_RAG_VECTOR_DIR=/models/rag-vectors \
    TZCHECK_DATA_DIR=/refdata

RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu-core libarchive-tools curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --retries 10 --timeout 120 -r requirements.txt

ARG WITH_OCR=0
COPY requirements-ocr.txt .
RUN if [ "$WITH_OCR" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/* \
      && pip install --retries 10 --timeout 120 -r requirements-ocr.txt; fi

ARG WITH_RAG=0
COPY requirements-rag.txt .
RUN if [ "$WITH_RAG" = "1" ]; then \
      pip install --retries 10 --timeout 120 --index-url https://download.pytorch.org/whl/cpu torch \
      && pip install --retries 10 --timeout 120 -r requirements-rag.txt; fi

ARG WITH_BROWSER=0
RUN if [ "$WITH_BROWSER" = "1" ]; then pip install playwright==1.58.0 && python -m playwright install --with-deps chromium; fi

COPY tzcheck ./tzcheck
COPY samples ./samples
COPY eval/eval_labels.py eval/synth_defects.py eval/labels.csv ./eval/
COPY data/norm_corpus ./data/norm_corpus

# справочники в /refdata (том refdata, его обновляет updater); профили характеристик и заказчиков остаются в коде
RUN mkdir -p /refdata && cp -r tzcheck/data/. /refdata/
# индекс BM25 по корпусу норм: 13 775 пунктов, около 10 с
RUN TZCHECK_REFDATA_OFFLINE=1 python -m tzcheck.rag build
# модели PaddleOCR (около 100 МБ) скачиваются при сборке; если сети нет, они скачаются при первом скане
RUN if [ "$WITH_OCR" = "1" ]; then python -c "from tzcheck import ocr; ocr.engine()" || echo "OCR: модели не скачаны, скачаются при первом скане"; fi

RUN mkdir -p /data/runs /models
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "tzcheck.api:app", "--host", "0.0.0.0", "--port", "8000"]
