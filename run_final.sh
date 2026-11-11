#!/usr/bin/env bash
# Cool-machine finalists run -> memory lifecycle -> streaming simulation, back to back.
cd "$(dirname "$0")"
echo "[$(date +%T)] 1/3 finalists"; .venv/bin/python run.py --config configs/finalists.yaml --tag finalists
echo "[$(date +%T)] 2/3 memory";    .venv/bin/python run_mem.py --config configs/finalists.yaml
echo "[$(date +%T)] 3/3 streaming"; .venv/bin/python run_stream.py --limit-per-cat 12 --tag final
echo "[$(date +%T)] ALL DONE"
