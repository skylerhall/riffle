import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from cascade import CX, CZ, H, RZ, X, Cascade, CascadeError, NoCloningError, Z  # noqa: E402
import mitigate as M  # noqa: E402


def approx(d, ref):
    keys = set(d) | set(ref)
    return all(abs(d.get(k, 0) - ref.get(k, 0)) < 1e-9 for k in keys)


def test_out_of_order_bell_pair():
    c = Cascade(2)
    c["entangle"] = CX("spread", "q1")
    assert c.result is None and "spread" in c.status   # waits for the missing node
    c["spread"] = H("q0")
    assert approx(c.result.probabilities, {"00": 0.5, "11": 0.5})


def test_interference_and_rewiring():
    c = Cascade(1)
    c["a"] = H("q0")
    c["b"] = H("a")
    assert approx(c.result.probabilities, {"0": 1})
    with c.batch():
        c["flip"] = Z("a")
        c["b"] = H("flip")
    assert approx(c.result.probabilities, {"1": 1})


def test_rotation_dial():
    c = Cascade(1)
    with c.batch():
        c["a"] = H("q0"); c["dial"] = RZ(math.pi / 2, "a"); c["out"] = H("dial")
    assert approx(c.result.probabilities, {"0": 0.5, "1": 0.5})


def test_grover_finds_11():
    g = Cascade(2)
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
    c = Cascade(2)
    c["a"] = H("q0")
    c["copy1"] = X("a")
    with pytest.raises(NoCloningError):
        c["copy2"] = X("a")
    assert "copy2" not in c.nodes and c.result is not None


def test_cycle_is_reported():
    c = Cascade(1)
    with c.batch():
        c["a"] = H("b")
        c["b"] = H("a")
    assert c.result is None and "cycle" in c.status


def test_bad_qubit_reference():
    c = Cascade(1)
    with pytest.raises(CascadeError):
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
