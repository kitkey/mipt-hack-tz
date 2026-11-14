FROM tzcheck-rag-models:local
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu-core libarchive-tools && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --retries 10 --timeout 120 --index-url https://pypi.org/simple -r /tmp/requirements.txt
COPY tzcheck /app/tzcheck
COPY samples /app/samples
EXPOSE 8000
CMD ["uvicorn", "tzcheck.api:app", "--host", "0.0.0.0", "--port", "8000"]
