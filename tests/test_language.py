"""The Riffle language front end: parsing, building, errors with line numbers."""
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import riffle_lang as L  # noqa: E402
import run_ibm  # noqa: E402


def run(src, name=None):
    circuits = L.parse(src)
    return L.build(circuits[name] if name else next(iter(circuits.values()))).run().probabilities


def same(a, b):
    return all(abs(a.get(k, 0) - b.get(k, 0)) < 1e-9 for k in set(a) | set(b))


@pytest.mark.parametrize("example,preset", [
    ("bell", "bell"), ("grover", "grover"), ("grover_work", "grover_work"),
    ("dial", "dial"), ("ghz", "ghz"), ("rainbow", "rainbow"),
])
def test_examples_match_the_python_presets(example, preset):
    c = L.load(ROOT / "examples" / f"{example}.rfl")
    assert same(c.run().probabilities, run_ibm.build(preset)[1].run().probabilities)


def test_no_cleanup_circuit_in_the_same_file():
    c = L.load(ROOT / "examples" / "grover_work.rfl", circuit="grover_no_cleanup")
    assert abs(c.run().probabilities["11"] - 0.625) < 1e-9


def test_order_does_not_matter():
    a = run("circuit c(x, y) {\n  e1, e2 = CX s, y\n  s = H x\n}")
    b = run("circuit c(x, y) {\n  s = H x\n  e1, e2 = CX s, y\n}")
    assert same(a, b) and same(a, {"00": 0.5, "11": 0.5})


def test_angles():
    for text, theta in (("pi/2", math.pi / 2), ("0.25*pi", math.pi / 4), ("3pi/4", 3 * math.pi / 4),
                        ("-pi/8", -math.pi / 8), ("π", math.pi), ("1.2", 1.2)):
        p = run(f"circuit c(q) {{\n  r = RY({text}) q\n}}")
        assert abs(p.get("1", 0) - math.sin(theta / 2) ** 2) < 1e-9, text


def test_dot_outputs_and_named_outputs_agree():
    a = run("circuit c(x, y) {\n  s = H x\n  g = CX s, y\n  f = Z g.1\n}")
    b = run("circuit c(x, y) {\n  s = H x\n  g0, g1 = CX s, y\n  f = Z g1\n}")
    assert same(a, b)


def test_semicolons_comments_and_case():
    assert same(run("circuit c(q) { a = h q; b = x a  # comment\n}"), {"0": 0.5, "1": 0.5})


def error(src):
    with pytest.raises(L.RiffleSyntaxError) as e:
        L.build(next(iter(L.parse(src, "t.rfl").values())), "t.rfl")
    return e.value


@pytest.mark.parametrize("src,line,words", [
    ("circuit c(q0, q1) {\n a = H q0\n b = X a\n d = X a\n}", 4, "used twice"),
    ("circuit c(q0) {\n a = H q0\n b = X aa\n}", 3, "Did you mean 'a'"),
    ("circuit c(q0) {\n a = H q0\n a = X a\n}", 3, "already defined on line 2"),
    ("circuit c(q0, q1) {\n a = H b\n b = H a\n}", 3, "loop"),
    ("circuit c(q0, q1) work(w) {\n b = H w\n}", 2, "can't be undone automatically"),
    ("circuit c(x, y) {\n a, b = CX x, x\n}", 2, "same gate"),
    ("circuit c(q0, q1) work(s) {\n s0 = H q0\n s1 = H q1\n a0, a1, w = CCX s0, s1, s\n h0 = H a0\n m = CZ h0, w\n}", 4, "can't be cleaned up"),
])
def test_errors_point_at_the_right_line(src, line, words):
    e = error(src)
    assert e.line == line and words in e.msg, (e.line, e.msg)


@pytest.mark.parametrize("src,line,words", [
    ("circuit c(q0) {\n a = HH q0\n}", 2, "Did you mean H"),
    ("circuit c(q0) {\n a = RZ q0\n}", 2, "needs an angle"),
    ("circuit c(q0, q1) {\n a, b = CX q0\n}", 2, "takes 2 inputs"),
    ("circuit c(q0) {\n a = H q0\n", 3, "missing its closing"),
    ("circuit c(q0) {\n a = H q0 q0\n}", 2, "one gate per line"),
])
def test_syntax_errors(src, line, words):
    with pytest.raises(L.RiffleSyntaxError) as e:
        L.parse(src)
    assert e.value.line == line and words in e.value.msg, (e.value.line, e.value.msg)


@pytest.mark.parametrize("preset", list(run_ibm.PRESETS))
def test_python_to_riffle_and_back(preset):
    c = run_ibm.build(preset)[1]
    back = L.build(L.parse(L.to_source(c, preset))[preset])
    assert same(c.run().probabilities, back.run().probabilities)


def test_formatter_is_stable():
    src = (ROOT / "examples" / "ghz.rfl").read_text()
    once = "".join(L.format_circuit(c) for c in L.parse(src).values())
    twice = "".join(L.format_circuit(c) for c in L.parse(once).values())
    assert once == twice


def test_command_line(capsys):
    assert L.main(["run", str(ROOT / "examples" / "grover.rfl")]) == 0
    assert "100.0%" in capsys.readouterr().out
    assert L.main(["build", str(ROOT / "examples" / "bell.rfl"), "--qasm"]) == 0
    assert "OPENQASM 3.0" in capsys.readouterr().out
