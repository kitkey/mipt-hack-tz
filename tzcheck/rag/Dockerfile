FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir 'pymorphy3>=2.0' 'pymorphy3-dicts-ru>=2.4'
COPY tzcheck ./tzcheck
ENV TZCHECK_REFDATA_OFFLINE=1
CMD ["python", "-m", "tzcheck.rag", "build"]
