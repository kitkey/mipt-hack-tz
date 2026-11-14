#!/usr/bin/env sh
# Демонстрация: поднять сервис в Docker, проверить ТЗ через API и CLI, сравнить версии.
# Модель и ключ задаются в .env (README, «Быстрый старт»); без них проверяются только оформление и безопасность.
set -e
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
# порт тот же, что берёт docker compose: из окружения, иначе строка APP_PORT в .env, иначе 8000
PORT="${APP_PORT:-$( [ -f .env ] && sed -n 's/^[[:space:]]*APP_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' .env | tail -n 1 )}"
PORT="${PORT:-8000}"
URL="http://localhost:$PORT"
OUT=demo/out
mkdir -p "$OUT"

echo "1. Сборка и запуск"
docker compose up -d --build app
until curl -fsS "$URL/health" >/dev/null 2>&1; do sleep 2; done
curl -fsS "$URL/health"; echo

summary() {
  docker compose exec -T app python -c "import json,sys; r=json.load(sys.stdin); print(r.get('file'), r.get('status'), 'замечаний', len(r.get('findings', [])), r.get('stats', {}).get('by_severity')); print(r.get('summary') or r.get('error'))"
}

echo "2. ТЗ на работы (лифт): API /api/check"
curl -fsS -F "public=1" -F "file=@samples/lift_src.docx" "$URL/api/check" -o "$OUT/lift_src.json"
summary < "$OUT/lift_src.json"

echo "3. ТЗ на поставку (метрология)"
curl -fsS -F "public=1" -F "file=@samples/metro_src.docx" "$URL/api/check" -o "$OUT/metro_src.json"
summary < "$OUT/metro_src.json"

echo "4. Исправленное ТЗ (лифт, итоговое)"
curl -fsS -F "public=1" -F "file=@samples/lift_fin.docx" "$URL/api/check" -o "$OUT/lift_fin.json"
summary < "$OUT/lift_fin.json"

echo "5. Сравнение версий: лифт исходный и итоговый (с внешней моделью нужен TZCHECK_DOC_PUBLIC=1 в .env или слой NER)"
curl -fsS -F "old=@samples/lift_src.docx" -F "new=@samples/lift_fin.docx" "$URL/api/compare?use_llm=true" -o "$OUT/compare_lift.json"
docker compose exec -T app python -c "import json,sys; r=json.load(sys.stdin); print(r.get('summary') or r)" < "$OUT/compare_lift.json"

echo "6. Повреждённый файл: ответ 400 с причиной"
: > "$OUT/empty.docx"
curl -sS -F "file=@$OUT/empty.docx" "$URL/api/check" -o "$OUT/empty.json" -w "HTTP %{http_code}\n"
summary < "$OUT/empty.json"

echo "7. CLI в контейнере: JSON, HTML и docx с комментариями Word"
docker compose exec -T -e TZCHECK_DOC_PUBLIC=1 app python -m tzcheck samples/lift_src.docx -o /tmp/demo
docker compose cp app:/tmp/demo "$OUT/cli"
ls "$OUT/cli"

echo "Готово. Веб-интерфейс: $URL , отчёты: $OUT/"
