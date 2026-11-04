"""
Riffle's noise-fighting compiler.

Three techniques, each one switchable, all measured against the default:

1. Noise-aware placement: transpile the circuit many ways and keep the version
   whose qubits and gates have the lowest error rates in the machine's *live*
   calibration data (scored on the whole compiled circuit, readout included).
2. Readout correction: run two extra calibration circuits on the exact same
   physical qubits (all 0s, all 1s), measure how often each qubit is misread,
   and undo that bias in the results.
3. Dynamical decoupling: ask IBM to fill idle moments with pulse pairs that
   cancel drift (real hardware only).
"""
from __future__ import annotations

import math

import numpy as np


# ------------------------------------------------------------- 1. placement
def op_error(target, name, qubits):
    try:
        props = target[name][tuple(qubits)]
    except KeyError:
        return 0.0
    return (props.error or 0.0) if props is not None else 0.0


def estimated_success(isa, target):
    """Chance that no gate or readout fails, from the machine's calibration data."""
    logp = 0.0
    for inst in isa.data:
        name = inst.operation.name
        if name in ("barrier", "delay"):
            continue
        qs = [isa.find_bit(q).index for q in inst.qubits]
        err = min(op_error(target, name, qs), 0.999)
        logp += math.log1p(-err)
    return math.exp(logp)


def best_transpile(qc, backend, tries=24):
    """Default transpile vs. best of many seeds, scored on live error rates."""
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    target = backend.target
    default = generate_preset_pass_manager(backend=backend, optimization_level=3).run(qc)
    best, best_score = default, estimated_success(default, target)
    default_score = best_score
    for seed in range(tries):
        cand = generate_preset_pass_manager(backend=backend, optimization_level=3, seed_transpiler=seed).run(qc)
        s = estimated_success(cand, target)
        if s > best_score:
            best, best_score = cand, s
    return default, default_score, best, best_score


def physical_qubits(isa, n):
    """Physical qubit holding each logical qubit at measurement time."""
    return list(isa.layout.final_index_layout())[:n]


# ------------------------------------------------------------- 2. readout
def calibration_circuits(phys, backend):
    """All-0 and all-1 preparations on exactly these physical qubits."""
    from qiskit import QuantumCircuit
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    n = len(phys)
    pm = generate_preset_pass_manager(backend=backend, optimization_level=0, initial_layout=phys)
    zero = QuantumCircuit(n); zero.measure_all()
    one = QuantumCircuit(n); one.x(range(n)); one.measure_all()
    return [pm.run(zero), pm.run(one)]


def flip_rates(zero_bits, one_bits, n):
    """Per-qubit (P(read 1 | was 0), P(read 0 | was 1)). Bitstrings are q0-first."""
    e0 = [sum(b[i] == "1" for b in zero_bits) / len(zero_bits) for i in range(n)]
    e1 = [sum(b[i] == "0" for b in one_bits) / len(one_bits) for i in range(n)]
    return e0, e1


def correct_readout(probs, e0, e1):
    """Undo independent per-qubit readout errors. probs: dict bitstring(q0-first) -> p."""
    n = len(e0)
    v = np.zeros((2,) * n)
    for b, p in probs.items():
        v[tuple(int(c) for c in b)] = p
    for i in range(n):
        A = np.array([[1 - e0[i], e1[i]], [e0[i], 1 - e1[i]]])  # columns: true 0, true 1
        v = np.moveaxis(np.tensordot(np.linalg.inv(A), v, axes=([1], [i])), 0, i)
    v = np.clip(v.reshape(-1), 0, None)
    v /= v.sum()
    return {format(i, f"0{n}b"): float(p) for i, p in enumerate(v) if p > 1e-6}


def fidelity(ideal, measured):
    keys = set(ideal) | set(measured)
    return sum(math.sqrt(ideal.get(k, 0) * measured.get(k, 0)) for k in keys) ** 2
