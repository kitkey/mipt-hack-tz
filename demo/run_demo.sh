#!/usr/bin/env sh
# Демонстрация: поднять сервис в Docker, проверить ТЗ через API и CLI, сравнить версии, пересчитать качество.
set -e
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
PORT="${APP_PORT:-8000}"
URL="http://localhost:$PORT"
OUT=demo/out
[ -f .env ] || cp .env.example .env
mkdir -p "$OUT"

echo "1. Сборка и запуск"
docker compose up -d --build app
until curl -fsS "$URL/health" >/dev/null 2>&1; do sleep 2; done
curl -fsS "$URL/health"; echo

summary() {
  docker compose exec -T app python -c "import json,sys; r=json.load(sys.stdin); print(r.get('file'), r.get('status'), 'замечаний', len(r.get('findings', [])), r.get('stats', {}).get('by_severity')); print(r.get('summary') or r.get('error'))"
}

echo "2. ТЗ на работы (лифт): API /api/check"
curl -fsS -F "file=@samples/lift_src.docx" "$URL/api/check?use_llm=false" -o "$OUT/lift_src.json"
summary < "$OUT/lift_src.json"

echo "3. ТЗ на поставку (метрология)"
curl -fsS -F "file=@samples/metro_src.docx" "$URL/api/check?use_llm=false" -o "$OUT/metro_src.json"
summary < "$OUT/metro_src.json"

echo "4. Исправленное ТЗ (лифт, итоговое): ложных замечаний должно быть мало"
curl -fsS -F "file=@samples/lift_fin.docx" "$URL/api/check?use_llm=false" -o "$OUT/lift_fin.json"
summary < "$OUT/lift_fin.json"

echo "5. Сравнение версий: метрология исходная и итоговая"
curl -fsS -F "old=@samples/metro_src.docx" -F "new=@samples/metro_fin.docx" "$URL/api/compare" -o "$OUT/compare_metro.json"
docker compose exec -T app python -c "import json,sys; print(json.load(sys.stdin)['summary'])" < "$OUT/compare_metro.json"

echo "6. Повреждённый файл: ответ 400 с причиной"
: > "$OUT/empty.docx"
curl -sS -F "file=@$OUT/empty.docx" "$URL/api/check?use_llm=false" -o "$OUT/empty.json" -w "HTTP %{http_code}\n"
summary < "$OUT/empty.json"

echo "7. CLI в контейнере: JSON, HTML и docx с комментариями Word"
docker compose exec -T app python -m tzcheck samples/lift_src.docx -o /tmp/demo
docker compose cp app:/tmp/demo "$OUT/cli"
ls "$OUT/cli"

echo "8. Качество на 105 правках закупщиков"
docker compose exec -T -e TZCHECK_REFDATA_OFFLINE=1 app python eval/eval_labels.py --out /tmp/eval.json

echo "Готово. Веб-интерфейс: $URL , отчёты: $OUT/"
