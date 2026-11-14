# Демонстрация: поднять сервис в Docker, проверить ТЗ через API и CLI, сравнить версии, пересчитать качество.
$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Set-Location (Join-Path $PSScriptRoot "..")
$port = if ($env:APP_PORT) { $env:APP_PORT } else { "8000" }
$url = "http://localhost:$port"
$out = "demo/out"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
New-Item -ItemType Directory -Force $out | Out-Null

function Show-Summary($path) {
    Get-Content $path -Raw -Encoding UTF8 | docker compose exec -T app python -c "import json,sys; r=json.load(sys.stdin); print(r.get('file'), r.get('status'), 'замечаний', len(r.get('findings', [])), r.get('stats', {}).get('by_severity')); print(r.get('summary') or r.get('error'))"
}

Write-Host "1. Сборка и запуск"
docker compose up -d --build app
do { Start-Sleep 2; $ok = $true; try { Invoke-RestMethod "$url/health" | Out-Null } catch { $ok = $false } } until ($ok)
curl.exe -fsS "$url/health"; Write-Host ""

Write-Host "2. ТЗ на работы (лифт): API /api/check"
curl.exe -fsS -F "file=@samples/lift_src.docx" "$url/api/check?use_llm=false" -o "$out/lift_src.json"
Show-Summary "$out/lift_src.json"

Write-Host "3. ТЗ на поставку (метрология)"
curl.exe -fsS -F "file=@samples/metro_src.docx" "$url/api/check?use_llm=false" -o "$out/metro_src.json"
Show-Summary "$out/metro_src.json"

Write-Host "4. Исправленное ТЗ (лифт, итоговое): ложных замечаний должно быть мало"
curl.exe -fsS -F "file=@samples/lift_fin.docx" "$url/api/check?use_llm=false" -o "$out/lift_fin.json"
Show-Summary "$out/lift_fin.json"

Write-Host "5. Сравнение версий: метрология исходная и итоговая"
curl.exe -fsS -F "old=@samples/metro_src.docx" -F "new=@samples/metro_fin.docx" "$url/api/compare" -o "$out/compare_metro.json"
Get-Content "$out/compare_metro.json" -Raw -Encoding UTF8 | docker compose exec -T app python -c "import json,sys; print(json.load(sys.stdin)['summary'])"

Write-Host "6. Повреждённый файл: ответ 400 с причиной"
New-Item -ItemType File -Force "$out/empty.docx" | Out-Null
curl.exe -sS -F "file=@$out/empty.docx" "$url/api/check?use_llm=false" -o "$out/empty.json" -w "HTTP %{http_code}`n"
Show-Summary "$out/empty.json"

Write-Host "7. CLI в контейнере: JSON, HTML и docx с комментариями Word"
docker compose exec -T app python -m tzcheck samples/lift_src.docx -o /tmp/demo
docker compose cp app:/tmp/demo "$out/cli"
Get-ChildItem "$out/cli"

Write-Host "8. Качество на 105 правках закупщиков"
docker compose exec -T -e TZCHECK_REFDATA_OFFLINE=1 app python eval/eval_labels.py --out /tmp/eval.json

Write-Host "Готово. Веб-интерфейс: $url , отчёты: $out/"
