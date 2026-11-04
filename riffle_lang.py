"""
The Riffle language: .rfl files -> Riffle circuits.

This is the front end. It reads Riffle source, checks it, reports mistakes
by line number, and builds the same circuit graph the rest of Riffle compiles,
simulates, and exports to IBM (OpenQASM 3) and Quantinuum (Guppy/HUGR).
See LANGUAGE.md for the language itself.

Command line:
    riffle run examples/grover.rfl             simulate and print the result
    riffle check examples/grover.rfl           check for mistakes only
    riffle build examples/grover.rfl --qasm    print OpenQASM 3   (IBM and most hardware)
    riffle build examples/grover.rfl --guppy   print Guppy        (Quantinuum)
    riffle build examples/grover.rfl --hugr out.hugr   write a HUGR package
    riffle fmt examples/grover.rfl             reprint with aligned columns

(`riffle` is `python riffle_lang.py` until the package is installed with pip.)
"""
from __future__ import annotations

import argparse
import difflib
import math
import re
import sys
from dataclasses import dataclass, field

import riffle as C

# ------------------------------------------------------------------ gates
# name -> (python constructor, qubit inputs, takes an angle)
GATES = {
    "H": (C.H, 1, False), "X": (C.X, 1, False), "Y": (C.Y, 1, False), "Z": (C.Z, 1, False),
    "S": (C.S, 1, False), "T": (C.T, 1, False),
    "RX": (C.RX, 1, True), "RY": (C.RY, 1, True), "RZ": (C.RZ, 1, True),
    "CX": (C.CX, 2, False), "CZ": (C.CZ, 2, False), "SWAP": (C.SWAP, 2, False), "CCX": (C.CCX, 3, False),
}
ALIASES = {"CNOT": "CX", "TOFFOLI": "CCX"}
KEYWORDS = {"circuit", "work", "cleanup", "on", "off", "pi"}


class RiffleSyntaxError(Exception):
    """A mistake in Riffle source, with the line it's on."""

    def __init__(self, line, msg, path=None):
        self.line, self.msg, self.path = line, msg, path
        where = f"{path}:" if path else ""
        super().__init__(f"{where}line {line}: {msg}" if line else f"{where}{msg}")


# ------------------------------------------------------------------ tokens
_TOKEN = re.compile(r"""
    (?P<comment>//[^\n]*|\#[^\n]*)
  | (?P<newline>\n)
  | (?P<space>[ \t\r]+)
  | (?P<number>\d+\.\d*|\d+)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*|π)
  | (?P<arrow>->)
  | (?P<op>[(){},=.*/;\-])
""", re.VERBOSE)


@dataclass
class Tok:
    kind: str
    text: str
    line: int


def tokenize(src, path=None):
    toks, line, pos = [], 1, 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise RiffleSyntaxError(line, f"unexpected character {src[pos]!r}", path)
        kind = m.lastgroup
        if kind == "newline":
            toks.append(Tok("nl", "\n", line)); line += 1
        elif kind in ("name", "number", "op", "arrow"):
            toks.append(Tok(kind, m.group(), line))
        pos = m.end()
    toks.append(Tok("eof", "", line))
    return toks


# ------------------------------------------------------------------ syntax tree
@dataclass
class Stmt:
    outputs: list[str]
    gate: str
    angle: float | None
    angle_src: str | None
    inputs: list[str]
    line: int


@dataclass
class CircuitDef:
    name: str
    qubits: list[str]
    work: list[str]
    line: int
    cleanup: bool = True
    stmts: list[Stmt] = field(default_factory=list)


# ------------------------------------------------------------------ parser
class Parser:
    def __init__(self, src, path=None):
        self.toks, self.i, self.path = tokenize(src, path), 0, path

    def err(self, msg, tok=None):
        return RiffleSyntaxError((tok or self.peek()).line, msg, self.path)

    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self):
        t = self.peek(); self.i += 1; return t

    def at(self, text):
        return self.peek().text == text

    def expect(self, text, what=None):
        t = self.peek()
        if t.text != text:
            shown = "end of line" if t.kind == "nl" else "end of file" if t.kind == "eof" else repr(t.text)
            raise self.err(f"expected {what or repr(text)}, found {shown}", t)
        return self.next()

    def skip_newlines(self):
        while self.peek().kind == "nl" or self.at(";"):
            self.next()

    def name(self, what):
        t = self.peek()
        if t.kind != "name" or t.text in KEYWORDS:
            shown = "end of line" if t.kind == "nl" else repr(t.text)
            raise self.err(f"expected {what}, found {shown}", t)
        return self.next().text

    def names(self, what):
        out = [self.name(what)]
        while self.at(","):
            self.next(); out.append(self.name(what))
        return out

    # file := circuit*
    def parse(self):
        circuits = {}
        self.skip_newlines()
        while self.peek().kind != "eof":
            c = self.circuit()
            if c.name in circuits:
                raise RiffleSyntaxError(c.line, f"circuit {c.name!r} is defined twice (first on line {circuits[c.name].line})", self.path)
            circuits[c.name] = c
            self.skip_newlines()
        if not circuits:
            raise RiffleSyntaxError(0, "no circuit found. A file needs at least one `circuit name(q0, ...) { ... }`", self.path)
        return circuits

    # circuit := 'circuit' NAME '(' names ')' ['work' '(' names ')'] '{' stmt* '}'
    def circuit(self):
        start = self.expect("circuit", "`circuit`")
        name = self.name("a circuit name")
        self.expect("(", "'(' and the circuit's qubits")
        qubits = [] if self.at(")") else self.names("a qubit name")
        self.expect(")")
        work = []
        if self.at("work"):
            self.next(); self.expect("(", "'(' and the work qubits")
            work = self.names("a work qubit name"); self.expect(")")
        if not qubits:
            raise self.err(f"circuit {name!r} needs at least one qubit", start)
        c = CircuitDef(name, qubits, work, start.line)
        self.expect("{", "'{' to open the circuit")
        self.skip_newlines()
        while not self.at("}"):
            if self.peek().kind == "eof":
                raise self.err(f"circuit {name!r} (line {start.line}) is missing its closing '}}'")
            self.statement(c)
            self.skip_newlines()
        self.expect("}")
        return c

    # stmt := names '=' GATE ['(' angle ')'] ref (',' ref)*  |  'cleanup' ('on'|'off')
    def statement(self, c):
        t = self.peek()
        if t.text == "cleanup":
            self.next()
            v = self.next()
            if v.text not in ("on", "off"):
                raise self.err("write `cleanup on` or `cleanup off`", v)
            c.cleanup = v.text == "on"
            return self.end_of_statement()
        outputs = self.names("a name for this gate's output")
        self.expect("=", "'=' (every line names what a gate produces: `a = H q0`)")
        gt = self.peek()
        if gt.kind != "name":
            raise self.err("expected a gate name such as H, CX or RZ", gt)
        gate = ALIASES.get(gt.text.upper(), gt.text.upper())
        if gate not in GATES:
            close = difflib.get_close_matches(gate, list(GATES) + list(ALIASES), n=1)
            hint = f" Did you mean {close[0]}?" if close else f" Gates: {', '.join(GATES)}."
            raise self.err(f"{gt.text!r} isn't a gate.{hint}", gt)
        self.next()
        _, arity, rotates = GATES[gate]
        angle = src = None
        if rotates:
            if not self.at("("):
                raise self.err(f"{gate} needs an angle, e.g. `{gate}(pi/2) {outputs[0]}`")
            self.next(); a0 = self.i
            angle = self.angle()
            src = " ".join(t.text for t in self.toks[a0:self.i])
            self.expect(")", "')' after the angle")
        elif self.at("("):
            raise self.err(f"{gate} doesn't take an angle")
        inputs = [self.ref()]
        while self.at(","):
            self.next(); inputs.append(self.ref())
        if len(inputs) != arity:
            raise self.err(f"{gate} takes {arity} input{'s' if arity > 1 else ''}, got {len(inputs)}", t)
        if len(outputs) not in (1, arity):
            raise self.err(f"{gate} has {arity} outputs: name just one (and use .0, .1 …) or name all {arity}", t)
        c.stmts.append(Stmt(outputs, gate, angle, src, inputs, t.line))
        self.end_of_statement()

    def end_of_statement(self):
        t = self.peek()
        if t.kind == "nl" or t.text in (";", "}") or t.kind == "eof":
            return
        raise self.err(f"unexpected {t.text!r}; one gate per line (or separate them with ';')", t)

    def ref(self):
        n = self.name("a qubit or a gate output")
        if self.at("."):
            self.next(); t = self.peek()
            if t.kind != "number" or not t.text.isdigit():
                raise self.err("expected an output number after '.', like both.2", t)
            n += "." + self.next().text
        return n

    # angle := ['-'] factor (('*'|'/') factor | factor)*      e.g. pi/2, 0.25*pi, 3pi/4, -0.5π
    def angle(self):
        sign = 1.0
        if self.at("-"):
            self.next(); sign = -1.0
        value = self.factor()
        while True:
            if self.at("*"):
                self.next(); value *= self.factor()
            elif self.at("/"):
                self.next(); d = self.factor()
                if d == 0:
                    raise self.err("division by zero in the angle")
                value /= d
            elif self.peek().text in ("pi", "π") or self.peek().kind == "number":
                value *= self.factor()          # 3pi, 0.5 pi
            else:
                return sign * value

    def factor(self):
        t = self.next()
        if t.kind == "number":
            return float(t.text)
        if t.text in ("pi", "π"):
            return math.pi
        raise RiffleSyntaxError(t.line, f"expected a number or pi in the angle, found {t.text!r}", self.path)


def parse(src, path=None):
    """Parse Riffle source into {circuit name: CircuitDef}."""
    return Parser(src, path).parse()


# ------------------------------------------------------------------ build
_WIRE = re.compile(r"'([^']+)'")


def build(cdef, path=None, shots=0, seed=None):
    """Turn a parsed circuit into a Riffle graph, reporting mistakes by line."""
    E = lambda line, msg: RiffleSyntaxError(line, msg, path)  # noqa: E731
    if len(cdef.qubits) != len(set(cdef.qubits)) or set(cdef.qubits) & set(cdef.work):
        raise E(cdef.line, "each qubit in the circuit header needs its own name")
    alias, line_of, node_of = {}, {}, {}
    for i, q in enumerate(cdef.qubits):
        alias[q] = f"q{i}"
    for k, w in enumerate(cdef.work):
        alias[w] = f"w{k}"
    for st in cdef.stmts:
        node = st.outputs[0]
        if C._is_source(node):
            raise E(st.line, f"{node!r} looks like a built-in qubit name (q0, w0…); pick another name")
        for i, out in enumerate(st.outputs):
            if out in alias:
                first = line_of.get(out)
                where = f"on line {first}" if first else "in the circuit header"
                raise E(st.line, f"{out!r} is already defined {where}. Each name holds one value; give this one a new name")
            alias[out] = node if len(st.outputs) == 1 else (node if i == 0 else f"{node}.{i}")
            line_of[out] = st.line
        node_of[node] = st
    names_by_internal = {v: k for k, v in alias.items()}

    def resolve(ref, st):
        base, dot, idx = ref.partition(".")
        if base not in alias:
            close = difflib.get_close_matches(base, list(alias), n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise E(st.line, f"{base!r} isn't defined anywhere in circuit {cdef.name!r}.{hint}")
        target = alias[base]
        if not dot:
            return target
        owner = node_of.get(target)
        if owner is None or len(owner.outputs) != 1:
            raise E(st.line, f"{ref!r}: use .N only on a gate whose outputs share one name")
        if int(idx) >= GATES[owner.gate][1]:
            raise E(st.line, f"{base!r} is a {owner.gate} with {GATES[owner.gate][1]} output(s); there's no .{idx}")
        return f"{target}.{idx}"

    c = C.Circuit(len(cdef.qubits), shots=shots, seed=seed, work=len(cdef.work), cleanup=cdef.cleanup)
    c.source = cdef
    c.line_names = list(cdef.qubits) + list(cdef.work)
    c.user_name = lambda internal: names_by_internal.get(internal, internal)

    def explain(msg):
        # translate internal wire names back to the user's names and add line numbers
        def user(w):
            n = names_by_internal.get(w)
            if n is None and "." in w:
                b, i = w.split(".")
                n = names_by_internal.get(b, b) + "." + i
            return n or w
        lines = [node_of[w.split(".")[0].replace("~undo", "")].line for w in _WIRE.findall(msg)
                 if w.split(".")[0].replace("~undo", "") in node_of]
        text = _WIRE.sub(lambda m: f"'{user(m.group(1))}'", msg)
        return (max(lines) if lines else cdef.line), text

    try:
        with c.batch():
            for st in cdef.stmts:
                fn, _, rotates = GATES[st.gate]
                args = [resolve(r, st) for r in st.inputs]
                c[st.outputs[0]] = fn(st.angle, *args) if rotates else fn(*args)
    except C.NoCloningError as e:
        m = re.search(r"wire '([^']+)' feeds both '([^']+)' and '([^']+)'", str(e))
        if not m:
            raise E(cdef.line, str(e)) from None
        wire, a, b = m.groups()
        wn = names_by_internal.get(wire) or names_by_internal.get(wire.split(".")[0], wire)
        if a == b:
            raise E(node_of[b].line, f"{wn!r} is used twice by the same gate. A qubit's state can't be copied, "
                    "so a gate's inputs must all be different values") from None
        raise E(node_of[b].line, f"{wn!r} is used twice: by line {node_of[a].line} and by this line. "
                f"A qubit's state can't be copied, so each value can be used only once") from None
    if c.result is None:
        ln, text = explain(_quote_names(c.status, node_of))
        if "can't be cleaned up automatically" in text:
            # point at the gate that writes into a work qubit
            ln = next((st.line for st in cdef.stmts if any(r.split(".")[0] in cdef.work for r in st.inputs)), ln)
        raise E(ln, _friendly(text, cdef)) from None
    return c


def _quote_names(msg, node_of):
    # Riffle's messages quote some names and not others; quote node names so they can be mapped back
    for n in sorted(node_of, key=len, reverse=True):
        msg = re.sub(rf"(?<!['\w]){re.escape(n)}(?![\w'])", f"'{n}'", msg)
    return msg


def _friendly(msg, cdef):
    for k, w in enumerate(cdef.work):
        msg = re.sub(rf"\bw{k}\b", w, msg)
    for i, q in enumerate(cdef.qubits):
        msg = re.sub(rf"\bq{i}\b", q, msg)
    return msg.replace("cycle between", "these gates feed each other in a loop")


def load(path, circuit=None, shots=0, seed=None):
    """Read a .rfl file and build one of its circuits (the first, unless named)."""
    src = open(path, encoding="utf-8").read()
    circuits = parse(src, path)
    if circuit is None:
        cdef = next(iter(circuits.values()))
    elif circuit in circuits:
        cdef = circuits[circuit]
    else:
        raise RiffleSyntaxError(0, f"no circuit named {circuit!r}; this file has: {', '.join(circuits)}", path)
    return build(cdef, path, shots=shots, seed=seed)


# ------------------------------------------------------------------ formatting
def format_circuit(cdef):
    """Canonical source for a circuit: aligned '=' columns, angles kept as written."""
    head = f"circuit {cdef.name}({', '.join(cdef.qubits)})" + (f" work({', '.join(cdef.work)})" if cdef.work else "") + " {"
    rows = []
    for st in cdef.stmts:
        lhs = ", ".join(st.outputs)
        rhs = st.gate + (f"({st.angle_src})" if st.angle_src else "") + " " + ", ".join(st.inputs)
        rows.append((lhs, rhs))
    width = max((len(l) for l, _ in rows), default=0)
    body = [f"    {l.ljust(width)} = {r}" for l, r in rows]
    if not cdef.cleanup:
        body.insert(0, "    cleanup off")
    return "\n".join([head, *body, "}"]) + "\n"


def to_source(c, name="main"):
    """Riffle source for a circuit built in Python (the reverse direction)."""
    qs = [f"q{i}" for i in range(c.n)]
    ws = [f"w{k}" for k in range(getattr(c, "work", 0))]
    rows = []
    for node, g in c.nodes.items():
        arity = len(g.inputs)
        ang = f"({_angle_text(g.params[0])})" if g.params else ""
        rows.append((node, f"{g.name}{ang} " + ", ".join(g.inputs), arity))
    width = max((len(n) for n, *_ in rows), default=0)
    head = f"circuit {name}({', '.join(qs)})" + (f" work({', '.join(ws)})" if ws else "") + " {"
    body = ([] if getattr(c, "cleanup", True) else ["    cleanup off"]) + [f"    {n.ljust(width)} = {r}" for n, r, _ in rows]
    return "\n".join([head, *body, "}"]) + "\n"


def _angle_text(theta):
    f = theta / math.pi
    for num in range(-16, 17):
        for den in (1, 2, 3, 4, 6, 8):
            if abs(f - num / den) < 1e-12:
                if num == 0:
                    return "0"
                n = "" if abs(num) == 1 else str(abs(num))
                s = ("-" if num < 0 else "") + (f"{n}pi" if n else "pi")
                return s if den == 1 else f"{s}/{den}"
    return repr(theta)


# ------------------------------------------------------------------ command line
def main(argv=None):
    ap = argparse.ArgumentParser(prog="riffle", description="The Riffle quantum circuit language.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, desc in (("run", "simulate a circuit and print the result"),
                       ("check", "check a file for mistakes"),
                       ("build", "compile to another format"),
                       ("fmt", "reprint a file with aligned columns")):
        p = sub.add_parser(name, help=desc)
        p.add_argument("file")
        p.add_argument("-c", "--circuit", help="which circuit in the file (default: the first)")
        if name == "run":
            p.add_argument("--shots", type=int, default=0, help="also sample this many measurements")
            p.add_argument("--seed", type=int, default=None)
        if name == "build":
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--qasm", action="store_true", help="OpenQASM 3 (IBM and most hardware)")
            g.add_argument("--guppy", action="store_true", help="Guppy (Quantinuum)")
            g.add_argument("--hugr", metavar="OUT", help="write a HUGR package (needs guppylang)")
            g.add_argument("--python", action="store_true", help="the same circuit as riffle.py code")
        if name == "fmt":
            p.add_argument("-w", "--write", action="store_true", help="rewrite the file in place")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fmt":
            src = open(a.file, encoding="utf-8").read()
            out = "\n".join(format_circuit(c) for c in parse(src, a.file).values())
            if a.write:
                open(a.file, "w", encoding="utf-8").write(out)
            else:
                sys.stdout.write(out)
            return 0
        c = load(a.file, a.circuit, shots=getattr(a, "shots", 0), seed=getattr(a, "seed", None))
        cdef = c.source
        if a.cmd == "check":
            extra = f" + {len(cdef.work)} work qubit(s)" if cdef.work else ""
            print(f"{a.file}: circuit {cdef.name!r} is fine: {len(cdef.stmts)} gates on {len(cdef.qubits)} qubit(s){extra}")
        elif a.cmd == "run":
            print(f"circuit {cdef.name}: {len(cdef.stmts)} gates on {len(cdef.qubits)} qubit(s) "
                  f"({', '.join(cdef.qubits)})" + (f" + work ({', '.join(cdef.work)})" if cdef.work else ""))
            print(c)
        elif a.cmd == "build":
            if a.qasm:
                print(to_qasm(c))
            elif a.python:
                print(to_python(c))
            else:
                import quantinuum as Q
                if a.guppy:
                    print(Q.to_guppy(c, name=cdef.name))
                else:
                    Q.to_hugr(c, a.hugr)
                    print(f"wrote {a.hugr}")
        return 0
    except (RiffleSyntaxError, C.RiffleError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def to_qasm(c):
    T = c.n + getattr(c, "work", 0)
    out = ["OPENQASM 3.0;", 'include "stdgates.inc";', "", f"qubit[{T}] q;", f"bit[{c.n}] c;", ""]
    for node, g, qs in c.compile():
        par = f"({g.params[0]!r})" if g.params else ""
        note = "cleanup, added by Riffle" if getattr(g, "auto", False) else c.user_name(node) if hasattr(c, "user_name") else node
        out.append(f"{g.name.lower()}{par} {', '.join(f'q[{q}]' for q in qs)};".ljust(28) + f"// {note}")
    out.append("")
    out += [f"c[{i}] = measure q[{i}];" for i in range(c.n)]
    return "\n".join(out)


def to_python(c):
    used = sorted({g.name for g in c.nodes.values()})
    out = ["from math import pi", f"from riffle import Circuit, {', '.join(used)}", "",
           f"c = Circuit({c.n}" + (f", work={c.work}" if getattr(c, 'work', 0) else "")
           + ("" if getattr(c, "cleanup", True) else ", cleanup=False") + ")", "with c.batch():"]
    for node, g in c.nodes.items():
        args = [repr(r) for r in g.inputs]
        if g.params:
            args.insert(0, repr(g.params[0]))
        out.append(f"    c[{node!r}] = {g.name}({', '.join(args)})")
    out.append("print(c)")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
