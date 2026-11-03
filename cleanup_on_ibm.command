#!/bin/bash
# Double-click on a Mac to run Grover through a work qubit on IBM hardware, with and without automatic cleanup.
cd "$(dirname "$0")" || exit 1
echo "== Cascade: running on IBM Quantum =="
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Python (takes a minute or two)..."
  python3 -m venv .venv || { echo "SETUP FAILED: python3 not available"; exit 1; }
fi
source .venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt || { echo "SETUP FAILED: install"; exit 1; }
mkdir -p results
python -W ignore run_ibm.py --only grover_work grover_work_raw --shots 4000 "$@" 2>&1 | tee results/last_cleanup.log
echo
echo "== Finished. You can close this window. =="
