#!/bin/bash
# bash eval_chain.sh <name> <model> <port> <server out file>: ждёт готовности сервера, гоняет A/B с записью GPU
NAME=$1; MODEL=$2; PORT=$3; OUT=$4
cd D:/mipt-hack-tz/eval/models_ab
until grep -q "^ready:" "$OUT" 2>/dev/null; do sleep 5; done
echo "SERVER_READY $(date '+%H:%M:%S')"
nohup D:/mipt-hack-tz/.venv/Scripts/python.exe gpu_sampler.py ${NAME}_gpu.csv > /dev/null 2>&1 &
D:/mipt-hack-tz/.venv/Scripts/python.exe smoke_test.py http://127.0.0.1:$PORT/v1 $MODEL > smoke_$NAME.txt 2>&1
bash ab_run.sh $NAME $MODEL $PORT > ${NAME}_run.log 2>&1
rm -f ${NAME}_gpu.csv.run
echo "EVAL_DONE $(date '+%H:%M:%S')"
