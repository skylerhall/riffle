import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from riffle import CX, CZ, H, RZ, X, Circuit, RiffleError, NoCloningError, Z  # noqa: E402
import mitigate as M  # noqa: E402


def approx(d, ref):
    keys = set(d) | set(ref)
    return all(abs(d.get(k, 0) - ref.get(k, 0)) < 1e-9 for k in keys)


def test_out_of_order_bell_pair():
    c = Circuit(2)
    c["entangle"] = CX("spread", "q1")
    assert c.result is None and "spread" in c.status   # waits for the missing node
    c["spread"] = H("q0")
    assert approx(c.result.probabilities, {"00": 0.5, "11": 0.5})


def test_interference_and_rewiring():
    c = Circuit(1)
    c["a"] = H("q0")
    c["b"] = H("a")
    assert approx(c.result.probabilities, {"0": 1})
    with c.batch():
        c["flip"] = Z("a")
        c["b"] = H("flip")
    assert approx(c.result.probabilities, {"1": 1})


def test_rotation_dial():
    c = Circuit(1)
    with c.batch():
        c["a"] = H("q0"); c["dial"] = RZ(math.pi / 2, "a"); c["out"] = H("dial")
    assert approx(c.result.probabilities, {"0": 0.5, "1": 0.5})


def test_grover_finds_11():
    g = Circuit(2)
    with g.batch():
        g["s0"], g["s1"] = H("q0"), H("q1")
        g["oracle"] = CZ("s0", "s1")
        g["h0"], g["h1"] = H("oracle.0"), H("oracle.1")
        g["x0"], g["x1"] = X("h0"), X("h1")
        g["mirror"] = CZ("x0", "x1")
        g["u0"], g["u1"] = X("mirror.0"), X("mirror.1")
        g["out0"], g["out1"] = H("u0"), H("u1")
    assert approx(g.result.probabilities, {"11": 1})


def test_no_cloning_is_rejected_and_rolled_back():
    c = Circuit(2)
    c["a"] = H("q0")
    c["copy1"] = X("a")
    with pytest.raises(NoCloningError):
        c["copy2"] = X("a")
    assert "copy2" not in c.nodes and c.result is not None


def test_cycle_is_reported():
    c = Circuit(1)
    with c.batch():
        c["a"] = H("b")
        c["b"] = H("a")
    assert c.result is None and "cycle" in c.status


def test_bad_qubit_reference():
    c = Circuit(1)
    with pytest.raises(RiffleError):
        c["a"] = H("q3")
        c.compile()


def test_readout_correction_recovers_truth():
    # true state: 50/50 between 00 and 11; qubit 0 misreads 0->1 at 5%, 1->0 at 3%
    e0, e1 = [0.05, 0.02], [0.03, 0.04]
    true = {"00": 0.5, "11": 0.5}
    noisy = {}
    for t, p in true.items():
        for out in ("00", "01", "10", "11"):
            q = p
            for i in range(2):
                if t[i] == "0":
                    q *= e0[i] if out[i] == "1" else 1 - e0[i]
                else:
                    q *= e1[i] if out[i] == "0" else 1 - e1[i]
            noisy[out] = noisy.get(out, 0) + q
    fixed = M.correct_readout(noisy, e0, e1)
    assert M.fidelity(true, noisy) < 0.95
    assert M.fidelity(true, fixed) > 0.9999


# ---------------------------------------------------------------- work qubits + automatic cleanup
from riffle import CCX  # noqa: E402


def _grover_via_work(cleanup=True):
    g = Circuit(2, work=1, cleanup=cleanup)
    with g.batch():
        g["s0"], g["s1"] = H("q0"), H("q1")
        g["both"] = CCX("s0", "s1", "w0")       # w0 = q0 AND q1
        g["mark"] = Z("both.2")                # phase flip through the work qubit
        g["h0"], g["h1"] = H("both.0"), H("both.1")
        g["x0"], g["x1"] = X("h0"), X("h1")
        g["mirror"] = CZ("x0", "x1")
        g["u0"], g["u1"] = X("mirror.0"), X("mirror.1")
        g["out0"], g["out1"] = H("u0"), H("u1")
    return g


def test_cleanup_restores_interference():
    g = _grover_via_work()
    assert approx(g.result.probabilities, {"11": 1})
    assert g.result.work_clean > 1 - 1e-9
    names = [name for name, *_ in g.compile()]
    assert "both~undo" in names
    assert names.index("both~undo") > names.index("mark")      # after the work qubit's last read
    assert names.index("both~undo") < names.index("h0")        # before the controls change
    assert "both~undo" not in g.nodes                          # the user's graph is untouched


def test_without_cleanup_the_garbage_spoils_it():
    g = _grover_via_work(cleanup=False)
    assert abs(g.result.probabilities["11"] - 0.625) < 1e-9
    assert abs(g.result.work_clean - 0.75) < 1e-9


def test_several_writes_are_undone_in_reverse():
    c = Circuit(2, work=1)
    with c.batch():
        c["a"], c["b"] = H("q0"), H("q1")
        c["flip"] = X("w0")                 # w0 = 1
        c["both"] = CCX("a", "b", "flip")   # w0 = NOT(a AND b)
        c["mark"] = Z("both.2")
    names = [name for name, *_ in c.compile()]
    assert names.index("both~undo") < names.index("flip~undo")
    assert c.result.work_clean > 1 - 1e-9
    assert approx(c.result.probabilities, {"00": 0.25, "01": 0.25, "10": 0.25, "11": 0.25})


def test_work_qubit_rules():
    c = Circuit(1, work=1)
    c["bad"] = H("w0")                       # H can't be undone by a classical uncompute
    assert c.result is None and "can't be undone automatically" in c.status

    c = Circuit(2, work=1)
    with c.batch():
        c["a"] = CX("q0", "w0")
        c["read"] = CZ("a.1", "q1")
        c["again"] = CX("a.0", "read.0")     # writes w0 after it was read
    assert c.result is None and "after" in c.status


def test_control_changed_before_last_read_is_rejected():
    c = Circuit(2, work=1)
    with c.batch():
        c["s0"], c["s1"] = H("q0"), H("q1")
        c["both"] = CCX("s0", "s1", "w0")
        c["h0"] = H("both.0")                # q0 changes ...
        c["mark"] = CZ("h0", "both.2")       # ... before w0 is read
    assert c.result is None and "can't be cleaned up" in c.status


def test_work_qubits_are_reserved_and_bounded():
    c = Circuit(1, work=1)
    with pytest.raises(RiffleError):
        c["w0"] = H("q0")
    with pytest.raises(RiffleError):
        c["a"] = X("w3")
        c.compile()
