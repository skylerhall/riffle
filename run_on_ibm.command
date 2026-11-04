#!/bin/bash
# Double-click on a Mac to run every Riffle preset on IBM's quantum computer.
cd "$(dirname "$0")" || exit 1
echo "== Riffle: running on IBM Quantum =="
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Python (takes a minute or two)..."
  python3 -m venv .venv || { echo "SETUP FAILED: python3 not available"; exit 1; }
fi
source .venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt || { echo "SETUP FAILED: install"; exit 1; }
mkdir -p results
python -W ignore run_ibm.py "$@" 2>&1 | tee results/last_run.log
echo
echo "== Finished. You can close this window. =="
