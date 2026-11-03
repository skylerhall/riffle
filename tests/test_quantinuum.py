"""Cascade -> Guppy -> HUGR -> Selene must agree with Cascade's own simulator.
Skipped when Quantinuum's tools aren't installed (pip install guppylang selene-sim)."""
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
pytest.importorskip("guppylang")
pytest.importorskip("selene_sim")

from cascade import CCX, CX, H, RX, RY, RZ, S, SWAP, T, X, Cascade  # noqa: E402
import quantinuum as Q  # noqa: E402

SHOTS = 4000


def close(c, tol=0.03):
    counts = Q.run_selene(c, shots=SHOTS, seed=11)
    ideal = c.run().probabilities
    for k in set(ideal) | set(counts):
        assert abs(ideal.get(k, 0) - counts.get(k, 0) / SHOTS) < tol, (k, ideal, counts)


def test_rotation_angles_match():
    # P(1) = sin^2(θ/2) for RX and RY: a wrong angle convention shows up immediately
    for gate, theta in ((RX, 0.3 * math.pi), (RY, 0.7 * math.pi)):
        c = Cascade(1)
        c["r"] = gate(theta, "q0")
        close(c)


def test_phase_rotation_interferes_correctly():
    c = Cascade(1)
    with c.batch():
        c["a"] = H("q0"); c["p"] = RZ(0.4 * math.pi, "a"); c["s"] = S("p"); c["t"] = T("s"); c["b"] = H("t")
    close(c)


def test_swap_is_a_relabel():
    c = Cascade(3)
    with c.batch():
        c["one"] = X("q0")
        c["sw"] = SWAP("one", "q2")        # the 1 moves to q2
        c["tag"] = CX("sw.1", "q1")        # and is used from its new line
    assert Q.run_selene(c, shots=200) == {"011": 200}
    assert "relabel" in Q.to_guppy(c)


def test_work_qubit_cleanup_survives_export():
    c = Cascade(2, work=1)
    with c.batch():
        c["s0"], c["s1"] = H("q0"), H("q1")
        c["both"] = CCX("s0", "s1", "w0")
        c["mark"] = RZ(math.pi, "both.2")
        c["h0"], c["h1"] = H("both.0"), H("both.1")
    src = Q.to_guppy(c)
    assert "cleanup, added by Cascade" in src and "discard(w0)" in src
    close(c)


def test_hugr_package_round_trips(tmp_path):
    c = Cascade(2)
    with c.batch():
        c["a"] = H("q0"); c["b"] = CX("a", "q1")
    path = tmp_path / "bell.hugr"
    pkg = Q.to_hugr(c, path)
    from hugr.package import Package

    again = Package.from_bytes(path.read_bytes())
    assert Q.hugr_summary(again) == Q.hugr_summary(pkg)
    assert Q.hugr_summary(pkg)["quantum_ops"] == {"CX": 1, "H": 1, "MeasureFree": 2, "QAlloc": 2}
