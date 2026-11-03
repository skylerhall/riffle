#!/bin/bash
# Double-click on a Mac to compare IBM's default compile with the Cascade compiler on real hardware.
cd "$(dirname "$0")" || exit 1
echo "== Cascade: running on IBM Quantum =="
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Python (takes a minute or two)..."
  python3 -m venv .venv || { echo "SETUP FAILED: python3 not available"; exit 1; }
fi
source .venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt || { echo "SETUP FAILED: install"; exit 1; }
mkdir -p results
python -W ignore run_ibm.py --compare "$@" 2>&1 | tee results/last_compare.log
echo
echo "== Finished. You can close this window. =="
