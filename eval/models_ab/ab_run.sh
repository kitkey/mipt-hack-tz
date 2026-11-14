#!/bin/bash
# A/B прогон на снимке кода: bash eval/models_ab/ab_run.sh <имя> <model_name сервера> <порт>
# Одинаковые настройки для всех моделей; снимок tzcheck в eval/models_ab/snap (код не меняется во время сравнения).
NAME=$1; MODEL=$2; PORT=$3
cd D:/mipt-hack-tz/eval/models_ab/snap
export TZCHECK_REFDATA_OFFLINE=1 PYTHONIOENCODING=utf-8
export TZCHECK_LLM_MODEL=openai/$MODEL TZCHECK_LLM_API_BASE=http://127.0.0.1:$PORT/v1 TZCHECK_LLM_API_KEY=local
export TZCHECK_LLM_WORKERS=1 TZCHECK_LLM_TIMEOUT=900 TZCHECK_LLM_MAX_TOKENS=6000 TZCHECK_LLM_TEMPERATURE=0.1
start=$(date +%s)
D:/mipt-hack-tz/.venv/Scripts/python.exe -W ignore eval/models_ab/run_eval.py --name $NAME --llm
echo "WALL_SECONDS $(( $(date +%s) - start ))"
