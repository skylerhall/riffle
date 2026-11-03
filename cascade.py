"""
cascade — a reactive, order-independent way to write quantum circuits.

Instead of writing a circuit top-to-bottom, you declare named nodes and
say what each node takes as input. Cascade builds the dependency graph,
sorts it, simulates it, and re-runs everything downstream whenever any
node changes (like a spreadsheet).

    c = Cascade(2)
    c.watch(print)
    c["bell"] = CX("spread", "q1")   # refers to a node that doesn't exist yet
    c["spread"] = H("q0")            # graph completes -> cascade runs

Inputs:
    "q0", "q1", ...   the starting qubits
    "name"            output of a one-qubit node
    "name.0", "name.1" outputs of a multi-qubit node (output i continues input i)

Because quantum states can't be copied, each wire can feed exactly one
node. Cascade enforces that rule (the no-cloning theorem) at compile time.
"""
from __future__ import annotations

import math
import re
from contextlib import contextmanager

import numpy as np


# ---------------------------------------------------------------- errors
class CascadeError(Exception):
    pass


class NoCloningError(CascadeError):
    pass


# ---------------------------------------------------------------- gates
_S2 = 1 / math.sqrt(2)
_X = np.array([[0, 1], [1, 0]], dtype=complex)
_Z = np.diag([1, -1]).astype(complex)


def _controlled(U):
    d = U.shape[0]
    M = np.eye(2 * d, dtype=complex)
    M[d:, d:] = U
    return M


class Gate:
    def __init__(self, name, matrix, inputs, params=()):
        self.name = name
        self.matrix = np.asarray(matrix, dtype=complex)
        self.inputs = tuple(inputs)
        self.params = tuple(params)
        arity = int(round(math.log2(self.matrix.shape[0])))
        if arity != len(self.inputs):
            raise CascadeError(f"{name} takes {arity} input(s), got {len(self.inputs)}")

    @property
    def arity(self):
        return len(self.inputs)

    def __repr__(self):
        p = "".join(f"{x:.3g}, " for x in self.params)
        return f"{self.name}({p}{', '.join(repr(i) for i in self.inputs)})"


def H(a): return Gate("H", np.array([[1, 1], [1, -1]]) * _S2, [a])
def X(a): return Gate("X", _X, [a])
def Y(a): return Gate("Y", [[0, -1j], [1j, 0]], [a])
def Z(a): return Gate("Z", _Z, [a])
def S(a): return Gate("S", np.diag([1, 1j]), [a])
def T(a): return Gate("T", np.diag([1, np.exp(1j * math.pi / 4)]), [a])


def RX(theta, a):
    c, s = math.cos(theta / 2), math.sin(theta / 2)
    return Gate("RX", [[c, -1j * s], [-1j * s, c]], [a], [theta])


def RY(theta, a):
    c, s = math.cos(theta / 2), math.sin(theta / 2)
    return Gate("RY", [[c, -s], [s, c]], [a], [theta])


def RZ(theta, a):
    return Gate("RZ", np.diag([np.exp(-0.5j * theta), np.exp(0.5j * theta)]), [a], [theta])


def CX(control, target): return Gate("CX", _controlled(_X), [control, target])
def CZ(a, b): return Gate("CZ", _controlled(_Z), [a, b])
def CCX(c1, c2, target): return Gate("CCX", _controlled(_controlled(_X)), [c1, c2, target])


def SWAP(a, b):
    M = np.eye(4, dtype=complex)[[0, 2, 1, 3]]
    return Gate("SWAP", M, [a, b])


# ---------------------------------------------------------------- result
class Result:
    def __init__(self, n, state, steps, shots=0, seed=None):
        self.n = n
        self.state = state.reshape(-1)
        self.steps = steps
        probs = np.abs(self.state) ** 2
        self.probabilities = {
            format(i, f"0{n}b"): float(p) for i, p in enumerate(probs) if p > 1e-9
        }
        self.counts = None
        if shots:
            rng = np.random.default_rng(seed)
            draws = rng.choice(len(probs), size=shots, p=probs / probs.sum())
            keys, vals = np.unique(draws, return_counts=True)
            self.counts = {format(int(k), f"0{n}b"): int(v) for k, v in zip(keys, vals)}

    def __str__(self):
        label = "q" + "".join(str(i) for i in range(self.n))
        lines = [f"  {label:<{self.n + 2}}  probability"]
        for bits, p in sorted(self.probabilities.items()):
            bar = "█" * round(p * 30)
            lines.append(f"  |{bits}>  {p:6.1%}  {bar}")
        if self.counts:
            lines.append("  shots: " + ", ".join(f"{k}:{v}" for k, v in sorted(self.counts.items())))
        return "\n".join(lines)


# ---------------------------------------------------------------- cascade
_NAME = re.compile(r"[A-Za-z_]\w*")
_QUBIT = re.compile(r"q(\d+)")


class Cascade:
    def __init__(self, n_qubits, shots=0, seed=None):
        self.n = n_qubits
        self.shots = shots
        self.seed = seed
        self.nodes: dict[str, Gate] = {}
        self._watchers = []
        self.result: Result | None = None
        self.status = "empty"
        self._batch_depth = 0

    # -- editing --------------------------------------------------------
    def __setitem__(self, name, gate):
        if not _NAME.fullmatch(name) or _QUBIT.fullmatch(name):
            raise CascadeError(f"bad node name {name!r} (q0, q1... are reserved)")
        if not isinstance(gate, Gate):
            raise CascadeError("node value must be a gate, e.g. H('q0')")
        before = dict(self.nodes)
        self.nodes[name] = gate
        self._commit(before)

    def __delitem__(self, name):
        before = dict(self.nodes)
        del self.nodes[name]
        self._commit(before)

    @contextmanager
    def batch(self):
        """Apply several edits as one atomic change (needed for rewiring):

            with c.batch():
                c["flip"] = Z("a")
                c["b"] = H("flip")
        """
        before = dict(self.nodes)
        self._batch_depth += 1
        try:
            yield self
        except BaseException:
            self.nodes = before
            raise
        finally:
            self._batch_depth -= 1
        self._commit(before)

    def _commit(self, before):
        if self._batch_depth:
            return
        try:
            self._cascade()
        except NoCloningError:
            self.nodes = before  # roll back so the graph stays physical
            raise

    def __getitem__(self, name):
        return self.nodes[name]

    def watch(self, fn):
        """Call fn(cascade) every time the circuit re-runs."""
        self._watchers.append(fn)
        if self.nodes:
            self._cascade()

    # -- compiling ------------------------------------------------------
    def _parse(self, ref):
        m = _QUBIT.fullmatch(ref)
        if m:
            q = int(m.group(1))
            if q >= self.n:
                raise CascadeError(f"{ref!r}: only {self.n} qubits exist")
            return (ref, 0)
        name, _, idx = ref.partition(".")
        return (name, int(idx) if idx else 0)

    def compile(self):
        """Return ordered steps [(node_name, gate, qubit_indices)].
        Raises CascadeError if the graph is incomplete or invalid."""
        missing, consumers, deps = set(), {}, {}
        for name, gate in self.nodes.items():
            deps[name] = set()
            for ref in gate.inputs:
                src, i = self._parse(ref)
                if not _QUBIT.fullmatch(src):
                    if src not in self.nodes:
                        missing.add(src)
                        continue
                    if i >= self.nodes[src].arity:
                        raise CascadeError(f"{name}: {src!r} has no output .{i}")
                    deps[name].add(src)
                wire = (src, i)
                if wire in consumers:
                    w = src if _QUBIT.fullmatch(src) or self.nodes[src].arity == 1 else f"{src}.{i}"
                    raise NoCloningError(
                        f"wire {w!r} feeds both {consumers[wire]!r} and {name!r} "
                        "— a qubit's state can't be copied"
                    )
                consumers[wire] = name
        if missing:
            raise CascadeError("waiting on: " + ", ".join(sorted(missing)))

        # topological sort (Kahn), stable by declaration order
        order, ready = [], [n for n in self.nodes if not deps[n]]
        remaining = {n: set(d) for n, d in deps.items()}
        while ready:
            n = ready.pop(0)
            order.append(n)
            for m in self.nodes:
                if n in remaining[m]:
                    remaining[m].discard(n)
                    if not remaining[m] and m not in order and m not in ready:
                        ready.append(m)
        if len(order) != len(self.nodes):
            stuck = [n for n in self.nodes if n not in order]
            raise CascadeError("cycle between: " + ", ".join(stuck))

        # follow each wire back to its physical qubit
        qubit_of = {(f"q{q}", 0): q for q in range(self.n)}
        steps = []
        for name in order:
            gate = self.nodes[name]
            qs = [qubit_of[self._parse(r)] for r in gate.inputs]
            if len(set(qs)) != len(qs):
                raise CascadeError(f"{name}: uses the same qubit twice")
            for i, q in enumerate(qs):
                qubit_of[(name, i)] = q
            steps.append((name, gate, qs))
        return steps

    # -- running --------------------------------------------------------
    def run(self):
        steps = self.compile()
        state = np.zeros((2,) * self.n, dtype=complex)
        state[(0,) * self.n] = 1
        for _, gate, qs in steps:
            k = len(qs)
            U = gate.matrix.reshape((2,) * (2 * k))
            state = np.tensordot(U, state, axes=(list(range(k, 2 * k)), qs))
            state = np.moveaxis(state, list(range(k)), qs)
        return Result(self.n, state, steps, self.shots, self.seed)

    def _cascade(self):
        try:
            self.result = self.run()
            self.status = "ok"
        except NoCloningError:
            raise
        except CascadeError as e:
            self.result, self.status = None, str(e)
        for fn in self._watchers:
            fn(self)

    # -- views ----------------------------------------------------------
    def diagram(self):
        steps = self.compile()
        rows = [f"q{q}: " for q in range(self.n)]
        sym = {"CX": ["●", "⊕"], "CZ": ["●", "●"], "CCX": ["●", "●", "⊕"], "SWAP": ["×", "×"]}
        for _, gate, qs in steps:
            marks = sym.get(gate.name, [gate.name] * len(qs))
            w = max(len(m) for m in marks) + 2
            lo, hi = min(qs), max(qs)
            for q in range(self.n):
                if q in qs:
                    cell = marks[qs.index(q)].center(w, "─")
                elif lo < q < hi:
                    cell = "│".center(w, "─")
                else:
                    cell = "─" * w
                rows[q] += cell
        return "\n".join(r + "─" for r in rows)

    def __str__(self):
        if self.result is None:
            return f"[cascade] {self.status}"
        return self.diagram() + "\n" + str(self.result)

    def to_qiskit(self):
        """Export to a Qiskit QuantumCircuit (requires `pip install qiskit`)."""
        from qiskit import QuantumCircuit

        qc = QuantumCircuit(self.n)
        for _, gate, qs in self.compile():
            m = getattr(qc, {"CX": "cx", "CZ": "cz", "CCX": "ccx", "SWAP": "swap"}.get(gate.name, gate.name.lower()))
            m(*gate.params, *qs)
        return qc
