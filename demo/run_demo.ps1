# Демонстрация: поднять сервис в Docker, проверить ТЗ через API и CLI, сравнить версии.
# Модель и ключ задаются в .env (README, «Быстрый старт»); без них проверяются только оформление и безопасность.
$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Set-Location (Join-Path $PSScriptRoot "..")
# порт тот же, что берёт docker compose: из окружения, иначе строка APP_PORT в .env, иначе 8000
$port = $env:APP_PORT
if (-not $port -and (Test-Path .env)) { $m = Select-String -Path .env -Pattern '^\s*APP_PORT\s*=\s*(\d+)' | Select-Object -Last 1; if ($m) { $port = $m.Matches[0].Groups[1].Value } }
if (-not $port) { $port = "8000" }
$url = "http://localhost:$port"
$out = "demo/out"
New-Item -ItemType Directory -Force $out | Out-Null

function Show-Summary($path) {
    Get-Content $path -Raw -Encoding UTF8 | docker compose exec -T app python -c "import json,sys; r=json.load(sys.stdin); print(r.get('file'), r.get('status'), 'замечаний', len(r.get('findings', [])), r.get('stats', {}).get('by_severity')); print(r.get('summary') or r.get('error'))"
}

Write-Host "1. Сборка и запуск"
docker compose up -d --build app
do { Start-Sleep 2; $ok = $true; try { Invoke-RestMethod "$url/health" | Out-Null } catch { $ok = $false } } until ($ok)
curl.exe -fsS "$url/health"; Write-Host ""

Write-Host "2. ТЗ на работы (лифт): API /api/check"
curl.exe -fsS -F "public=1" -F "file=@samples/lift_src.docx" "$url/api/check" -o "$out/lift_src.json"
Show-Summary "$out/lift_src.json"

Write-Host "3. ТЗ на поставку (метрология)"
curl.exe -fsS -F "public=1" -F "file=@samples/metro_src.docx" "$url/api/check" -o "$out/metro_src.json"
Show-Summary "$out/metro_src.json"

Write-Host "4. Исправленное ТЗ (лифт, итоговое)"
curl.exe -fsS -F "public=1" -F "file=@samples/lift_fin.docx" "$url/api/check" -o "$out/lift_fin.json"
Show-Summary "$out/lift_fin.json"

Write-Host "5. Сравнение версий: лифт исходный и итоговый (с внешней моделью нужен TZCHECK_DOC_PUBLIC=1 в .env или слой NER)"
curl.exe -fsS -F "old=@samples/lift_src.docx" -F "new=@samples/lift_fin.docx" "$url/api/compare?use_llm=true" -o "$out/compare_lift.json"
Get-Content "$out/compare_lift.json" -Raw -Encoding UTF8 | docker compose exec -T app python -c "import json,sys; r=json.load(sys.stdin); print(r.get('summary') or r)"

Write-Host "6. Повреждённый файл: ответ 400 с причиной"
New-Item -ItemType File -Force "$out/empty.docx" | Out-Null
curl.exe -sS -F "file=@$out/empty.docx" "$url/api/check" -o "$out/empty.json" -w "HTTP %{http_code}`n"
Show-Summary "$out/empty.json"

Write-Host "7. CLI в контейнере: JSON, HTML и docx с комментариями Word"
docker compose exec -T -e TZCHECK_DOC_PUBLIC=1 app python -m tzcheck samples/lift_src.docx -o /tmp/demo
docker compose cp app:/tmp/demo "$out/cli"
Get-ChildItem "$out/cli"

Write-Host "Готово. Веб-интерфейс: $url , отчёты: $out/"
