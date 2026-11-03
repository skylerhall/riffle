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

Work qubits and automatic cleanup:

    c = Cascade(2, work=1)              # w0 is a scratch qubit that starts at |0>
    c["both"] = CCX("a", "b", "w0")     # compute: w0 = a AND b
    c["mark"] = Z("both.2")             # use it
    # ...the compiler adds the undo of `both` right after w0's last use,
    # wired in before anything changes a or b again, so w0 returns to |0>
    # and doesn't spoil the interference. Cascade(..., cleanup=False) shows
    # what happens without it.

A work qubit may only be written by X, CX or CCX (all classical and
self-inverse), and only read afterwards: as a control, or by a phase gate
(Z, S, T, RZ, CZ). Anything else is a compile error, because no automatic
cleanup could be correct.
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
    def __init__(self, n, state, steps, shots=0, seed=None, work=0):
        self.n = n
        self.work = work
        self.state = state.reshape(-1)
        self.steps = steps
        full = np.abs(state) ** 2
        # work qubits are scratch space: report the main qubits only
        main = full.reshape((2 ** n, 2 ** work)).sum(axis=1) if work else full.reshape(-1)
        self.work_clean = float(full.reshape((2 ** n, 2 ** work))[:, 0].sum()) if work else 1.0
        probs = main
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
        if self.work:
            clean = "clean" if self.work_clean > 1 - 1e-9 else f"NOT clean ({self.work_clean:.1%} back at |0>)"
            lines.append(f"  work qubits: {clean}")
        return "\n".join(lines)


# ---------------------------------------------------------------- cascade
_NAME = re.compile(r"[A-Za-z_]\w*")
_QUBIT = re.compile(r"q(\d+)")
_WORK = re.compile(r"w(\d+)")

# For work-qubit cleanup: input slots that flip a work qubit (classical, self-inverse) ...
_WRITES = {"X": {0}, "CX": {1}, "CCX": {2}}
# ... and slots that leave a qubit's 0/1 value unchanged (controls and phase gates).
_READS = {"CX": {0}, "CCX": {0, 1}, "CZ": {0, 1}, "Z": {0}, "S": {0}, "T": {0}, "RZ": {0}}


def _is_source(ref):
    return bool(_QUBIT.fullmatch(ref) or _WORK.fullmatch(ref))


class Cascade:
    def __init__(self, n_qubits, shots=0, seed=None, work=0, cleanup=True):
        self.n = n_qubits
        self.work = work
        self.cleanup = cleanup
        self.shots = shots
        self.seed = seed
        self.nodes: dict[str, Gate] = {}
        self._watchers = []
        self.result: Result | None = None
        self.status = "empty"
        self._batch_depth = 0

    # -- editing --------------------------------------------------------
    def __setitem__(self, name, gate):
        if not _NAME.fullmatch(name) or _is_source(name):
            raise CascadeError(f"bad node name {name!r} (q0, q1... and w0, w1... are reserved)")
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
            if int(m.group(1)) >= self.n:
                raise CascadeError(f"{ref!r}: only {self.n} qubits exist")
            return (ref, 0)
        m = _WORK.fullmatch(ref)
        if m:
            if int(m.group(1)) >= self.work:
                raise CascadeError(f"{ref!r}: only {self.work} work qubits exist")
            return (ref, 0)
        name, _, idx = ref.partition(".")
        return (name, int(idx) if idx else 0)

    def compile(self):
        """Return ordered steps [(node_name, gate, qubit_indices)].
        Raises CascadeError if the graph is incomplete or invalid.
        With work qubits and cleanup on, the steps include the automatic
        undo gates (their names end in ~undo)."""
        steps = self._compile_nodes(self.nodes)
        if not (self.work and self.cleanup):
            return steps
        expanded = self._with_cleanup()
        try:
            return self._compile_nodes(expanded)
        except CascadeError as e:
            if "cycle" not in str(e):
                raise
            raise CascadeError(
                "a work qubit can't be cleaned up automatically: one of the qubits used to compute it "
                "is changed before the work qubit's last use. Read the work qubit first, or change "
                "that qubit later."
            ) from None

    def _compile_nodes(self, nodes):
        missing, consumers, deps = set(), {}, {}
        for name, gate in nodes.items():
            deps[name] = set()
            for ref in gate.inputs:
                src, i = self._parse(ref)
                if not _is_source(src):
                    if src not in nodes:
                        missing.add(src)
                        continue
                    if i >= nodes[src].arity:
                        raise CascadeError(f"{name}: {src!r} has no output .{i}")
                    deps[name].add(src)
                wire = (src, i)
                if wire in consumers:
                    w = src if _is_source(src) or nodes[src].arity == 1 else f"{src}.{i}"
                    raise NoCloningError(
                        f"wire {w!r} feeds both {consumers[wire]!r} and {name!r} "
                        "— a qubit's state can't be copied"
                    )
                consumers[wire] = name
        if missing:
            raise CascadeError("waiting on: " + ", ".join(sorted(missing)))

        # topological sort (Kahn), stable by declaration order
        order, ready = [], [n for n in nodes if not deps[n]]
        remaining = {n: set(d) for n, d in deps.items()}
        while ready:
            n = ready.pop(0)
            order.append(n)
            for m in nodes:
                if n in remaining[m]:
                    remaining[m].discard(n)
                    if not remaining[m] and m not in order and m not in ready:
                        ready.append(m)
        if len(order) != len(nodes):
            stuck = [n for n in nodes if n not in order]
            raise CascadeError("cycle between: " + ", ".join(stuck))

        # follow each wire back to its physical qubit (work qubits come after the main ones)
        qubit_of = {(f"q{q}", 0): q for q in range(self.n)}
        qubit_of.update({(f"w{k}", 0): self.n + k for k in range(self.work)})
        steps = []
        for name in order:
            gate = nodes[name]
            qs = [qubit_of[self._parse(r)] for r in gate.inputs]
            if len(set(qs)) != len(qs):
                raise CascadeError(f"{name}: uses the same qubit twice")
            for i, q in enumerate(qs):
                qubit_of[(name, i)] = q
            steps.append((name, gate, qs))
        return steps

    # -- automatic cleanup -----------------------------------------------
    def _with_cleanup(self):
        """Rewrite the graph so every work qubit returns to |0>.

        For each work qubit, its writes are undone in reverse order, right after
        its last read. Each undo gate is spliced into the control qubits' lines
        just before the first gate that would change them, so the controls still
        hold the values they had when the work qubit was computed."""
        nodes = dict(self.nodes)

        def consumers():
            return {self._parse(ref): (name, j) for name, g in nodes.items() for j, ref in enumerate(g.inputs)}

        def ref_of(name, i):
            return name if nodes[name].arity == 1 else f"{name}.{i}"

        def rewire(name, slot, ref):
            g = nodes[name]
            inputs = list(g.inputs); inputs[slot] = ref
            new = Gate(g.name, g.matrix, inputs, g.params)
            new.auto = getattr(g, "auto", False)
            nodes[name] = new

        for k in range(self.work):
            line, cons, wire = [], consumers(), (f"w{k}", 0)
            while wire in cons:
                wire = cons[wire]
                line.append(wire)
            writes, last_read = [], None
            for name, j in line:
                g = nodes[name].name
                if j in _WRITES.get(g, ()):
                    if last_read:
                        raise CascadeError(
                            f"w{k} is written by {name!r} after {last_read!r} has read it. "
                            "Automatic cleanup needs every write to a work qubit before its first read.")
                    writes.append((name, j))
                elif j in _READS.get(g, ()):
                    last_read = name
                else:
                    raise CascadeError(
                        f"{name!r} ({g}) changes work qubit w{k} in a way that can't be undone automatically. "
                        "Work qubits can only be written by X, CX or CCX, then read as a control or by a phase gate.")
            if not writes:
                continue
            end = line[-1]  # the work qubit's open end
            for name, j in reversed(writes):
                g = nodes[name]
                undo_name = f"{name}~undo"
                inputs, splice = [], []
                for s_ in range(g.arity):
                    if s_ == j:
                        inputs.append(ref_of(*end))
                        continue
                    # walk this control's line forward past gates that only read it
                    cons, w = consumers(), (name, s_)
                    while w in cons:
                        m, t = cons[w]
                        if t in _READS.get(nodes[m].name, ()):
                            w = (m, t)
                        else:
                            splice.append((m, t, s_))  # first gate that changes it: feed it the undo's output
                            break
                    inputs.append(ref_of(*w))
                undo = Gate(g.name, g.matrix, inputs, g.params)  # X, CX and CCX are their own inverse
                undo.auto = True
                nodes[undo_name] = undo
                for m, t, s_ in splice:
                    rewire(m, t, f"{undo_name}.{s_}" if undo.arity > 1 else undo_name)
                end = (undo_name, j)
        return nodes

    # -- running --------------------------------------------------------
    def run(self):
        steps = self.compile()
        total = self.n + self.work
        state = np.zeros((2,) * total, dtype=complex)
        state[(0,) * total] = 1
        for _, gate, qs in steps:
            k = len(qs)
            U = gate.matrix.reshape((2,) * (2 * k))
            state = np.tensordot(U, state, axes=(list(range(k, 2 * k)), qs))
            state = np.moveaxis(state, list(range(k)), qs)
        return Result(self.n, state, steps, self.shots, self.seed, work=self.work)

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
        total = self.n + self.work
        rows = [f"q{q}: " for q in range(self.n)] + [f"w{k}: " for k in range(self.work)]
        sym = {"CX": ["●", "⊕"], "CZ": ["●", "●"], "CCX": ["●", "●", "⊕"], "SWAP": ["×", "×"]}
        auto_sym = {"CX": ["○", "⊕"], "CCX": ["○", "○", "⊕"], "X": ["x"]}
        any_auto = False
        for _, gate, qs in steps:
            auto = getattr(gate, "auto", False)
            any_auto |= auto
            marks = (auto_sym if auto else sym).get(gate.name, [gate.name] * len(qs))
            w = max(len(m) for m in marks) + 2
            lo, hi = min(qs), max(qs)
            for q in range(total):
                if q in qs:
                    cell = marks[qs.index(q)].center(w, "─")
                elif lo < q < hi:
                    cell = "│".center(w, "─")
                else:
                    cell = "─" * w
                rows[q] += cell
        out = "\n".join(r + "─" for r in rows)
        if any_auto:
            out += "\n    (○ / x = added by Cascade to return work qubits to |0>)"
        return out

    def __str__(self):
        if self.result is None:
            return f"[cascade] {self.status}"
        return self.diagram() + "\n" + str(self.result)

    def to_qiskit(self, measure=False):
        """Export to a Qiskit QuantumCircuit (requires `pip install qiskit`).
        measure=True adds a 'meas' register reading the main qubits (not the work qubits)."""
        from qiskit import ClassicalRegister, QuantumCircuit

        qc = QuantumCircuit(self.n + self.work)
        for _, gate, qs in self.compile():
            m = getattr(qc, {"CX": "cx", "CZ": "cz", "CCX": "ccx", "SWAP": "swap"}.get(gate.name, gate.name.lower()))
            m(*gate.params, *qs)
        if measure:
            qc.add_register(ClassicalRegister(self.n, "meas"))
            qc.measure(range(self.n), range(self.n))
        return qc
