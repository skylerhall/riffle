# Cascade

**A reactive quantum circuit language: write gates in any order, the compiler enforces the physics, and you can hear the result. Tested on real IBM quantum hardware.**

![Cascade Listen](docs/listen.png)

Most quantum tools have you write circuits top to bottom, like assembly. In Cascade you
declare named gates and what they connect to, in any order. Cascade builds the
dependency graph, sorts it, simulates it, and re-runs everything downstream whenever
anything changes, the way a spreadsheet does.

```python
from cascade import Cascade, H, CX

c = Cascade(2)
c["entangle"] = CX("spread", "q1")   # refers to a gate that doesn't exist yet: Cascade waits
c["spread"]   = H("q0")              # graph complete: it runs
print(c)
```
```
q0: ─H──●──
q1: ────⊕──
  |00>   50.0%  ███████████████
  |11>   50.0%  ███████████████
```

The compiler knows quantum rules. A qubit's state can't be copied (the no-cloning theorem),
so wiring one output into two gates is a compile error and the edit is rolled back:

```
NoCloningError: wire 'a' feeds both 'copy1' and 'copy2' — a qubit's state can't be copied
```

### Work qubits with automatic cleanup

Many quantum algorithms need scratch qubits: compute something into a spare qubit, use it,
then put it back to |0⟩. Forget that last step and the leftover scratch value silently
ruins the interference the algorithm depends on. Most quantum languages leave this
to the programmer. Cascade does it for you:

```python
c = Cascade(2, work=1)               # w0 is a work qubit that starts at |0>
c["both"] = CCX("s0", "s1", "w0")    # compute: w0 = q0 AND q1
c["mark"] = Z("both.2")              # use it
# the compiler adds the undo of `both` right after w0's last use,
# spliced in before anything changes q0 or q1 again
```

The rules are checked at compile time. A work qubit may only be written by X, CX or CCX and then
only read (as a control, or by Z, S, T, RZ, CZ). Anything else, or changing a control qubit before
the work qubit's last read, is a compile error with an explanation, because no automatic cleanup
could be correct. `Cascade(..., cleanup=False)` shows what happens without it.

## Results on real quantum hardware

Every circuit here has run on IBM's 156-qubit Heron processors through the free
[IBM Quantum](https://quantum.cloud.ibm.com) Open Plan. Raw data is in [`results/`](results/).

### Cascade's compiler vs. IBM's default (ibm_fez, Nov 3 2026, 4,000 shots each, same session)

| Circuit | IBM default compile | Cascade compiler | Gain |
|---|---|---|---|
| Bell pair | 98.5% | **99.9%** | +1.3 pts |
| Grover search | 96.8% | **99.3%** | +2.4 pts |
| GHZ (3-qubit) | 96.2% | **97.5%** | +1.3 pts |

"Match" is the classical fidelity between measured and ideal outcome distributions.

The Cascade compiler ([`mitigate.py`](mitigate.py)) does three things:

1. **Noise-aware placement**: compiles each circuit 25 ways and keeps the one with the best
   predicted success from the machine's live calibration data. (On this run it chose the same
   qubits as IBM's compiler, so the gain came from the next two steps.)
2. **Readout correction**: runs two calibration circuits on the exact physical qubits used,
   measures each qubit's misread rates, and inverts that bias.
3. **Dynamical decoupling**: pulse pairs in idle moments to cancel drift.

### Automatic cleanup on real hardware (ibm_kingston, Nov 3 2026, 4,000 shots each)

Grover search marking its answer through a work qubit:

| | Found the answer |11⟩ | Work qubit back at |0⟩ (simulated) |
|---|---|---|
| **With automatic cleanup** | **92.0%** | 100% |
| Without cleanup | 59.2% | 75% |

Without cleanup even a perfect machine tops out at 62.5%; the hardware result tracks that.

### First run, all presets (ibm_kingston, Nov 3 2026, 2,000 shots, default compile)

| Circuit | Match with ideal |
|---|---|
| Interference dial | 100.0% |
| Phase rainbow (8 outcomes) | 100.0% |
| GHZ chord | 95.5% |
| Bell pair | 95.4% |
| Grover search | 91.9% |

## Runs on Quantinuum's stack too (HUGR)

Cascade compiles to two vendors' formats. Besides OpenQASM 3 for IBM, [`quantinuum.py`](quantinuum.py)
writes each circuit as a **Guppy** program, Quantinuum's quantum language, and lets Guppy's own
compiler turn it into **HUGR**, the graph-based format behind their Helios computer. The HUGR is
then run on Quantinuum's open-source **Selene** emulator and checked against Cascade's simulator:

| Circuit | HUGR nodes | Selene matches Cascade |
|---|---|---|
| Interference dial | 17 | 100.0% |
| Bell pair | 14 | 100.0% |
| Grover search | 24 | 100.0% |
| GHZ chord | 19 | 100.0% |
| Phase rainbow (8 outcomes) | 28 | 99.8% |
| Grover via work qubit (cleanup included) | 28 | 100.0% |

(2,000 shots each; the 99.8% is ordinary sampling noise across 8 equally likely outcomes.)

Guppy, like Cascade, treats qubits as values that can't be copied, so the two fit naturally:
each Cascade wire becomes one Guppy variable, and a SWAP becomes a renaming with no gates at all.
The exported `.hugr` packages and Guppy sources are in [`hugr/`](hugr/).

```bash
pip install guppylang selene-sim
python quantinuum.py        # export every preset to hugr/ and check each on Selene
```

## Cascade Listen

[`cascade_listen.html`](cascade_listen.html) is a browser instrument for the language. Open it in any
browser, no install needed. You build circuits by dragging wires between gates (or in a plain list view)
and hear the circuit as you go:

- each possible measurement outcome is a note (G major pentatonic);
- oscillator amplitude = |quantum amplitude|, so loudness = probability and total loudness never changes;
- phase, which measurement can't see, becomes color and stereo position, so you can watch and hear interference;
- the wire editor enforces the physics as you patch: plugging one output into two gates is refused
  (no-cloning), and so is wiring a loop;
- **Real** mode plays what IBM's machine actually measured, including the noise, and Measure mode
  replays the machine's shots in the order they came out.

It exports any patch to OpenQASM 3 (IBM), Guppy (Quantinuum), `cascade.py` code, or a patch file that `run_ibm.py` can send to IBM.

## Run it yourself

```bash
pip install -r requirements.txt
python demo.py                 # four short demos
python -m pytest tests         # test suite
```

### On a real IBM quantum computer (free)

1. Create a free account at [quantum.cloud.ibm.com](https://quantum.cloud.ibm.com) (Open Plan, us-east) and copy your API key.
2. Save the key as plain text in `ibm_key.txt` in this folder. It's in `.gitignore` and never leaves your machine.
3. On a Mac, double-click `run_on_ibm.command` (all presets), `compare_on_ibm.command`
   (default vs. Cascade compiler) or `cleanup_on_ibm.command` (work qubit with vs. without cleanup). Or from a terminal:

```bash
python run_ibm.py                        # all presets on the least busy machine
python run_ibm.py --compare              # IBM default vs. Cascade compiler, side by side
python run_ibm.py --patch my_patch.json  # your own circuit, exported from Cascade Listen
python run_ibm.py --fake                 # noise model of ibm_kingston, no account (pip install qiskit-aer)
python run_ibm.py --only grover_work grover_work_raw   # work-qubit Grover with and without cleanup
```

Results land in `results/`. Load them in Cascade Listen with **Load results**.
A full run uses a few seconds of the Open Plan's 10 free minutes per 28 days.

## Files

| File | What it is |
|---|---|
| `cascade.py` | The language: gates, graph compiler, work-qubit cleanup, statevector simulator, Qiskit export |
| `mitigate.py` | The noise-fighting compiler: placement scoring, readout correction |
| `quantinuum.py` | Export to Guppy and HUGR, and verification on Quantinuum's Selene emulator |
| `run_ibm.py` | Sends circuits to IBM Quantum and saves results |
| `cascade_listen.html` | The browser instrument |
| `demo.py` | Demos: out-of-order code, interference, Grover search, no-cloning |
| `tests/` | Test suite |
| `hugr/` | Every preset as a `.hugr` package plus its Guppy source |
| `results/` | Real hardware runs (plus one clearly labeled noise-model sample) |

## Where this is going

Cascade is an experiment in what a quantum language should feel like to write: graph-shaped,
live, and strict about physics at compile time. Next up:

- a run on Quantinuum's Helios hardware, which needs a Quantinuum Nexus account
- loops and mid-circuit measurement, which HUGR supports and Cascade doesn't express yet

## License

MIT © 2026 Skyler Hall
