"""Four short demos of cascade-style quantum coding. Run: python demo.py"""
from cascade import Cascade, CascadeError, NoCloningError, H, X, Z, CX, CZ


def show(title):
    print("\n" + "=" * 60 + f"\n{title}\n" + "=" * 60)


def printer(c):
    print(c, "\n")


# 1 ─ Declared out of order. Nothing runs until the graph is complete.
show("1. Bell state, declared backwards")
c = Cascade(2, shots=1000, seed=7)
c.watch(printer)
c["entangle"] = CX("spread", "q1")   # 'spread' doesn't exist yet -> waits
c["spread"] = H("q0")                # now it cascades

# 2 ─ Interference: change one upstream node, everything downstream re-runs.
show("2. Interference — the actual source of quantum power")
c = Cascade(1)
c["a"] = H("q0")
c["b"] = H("a")
print("H then H: the two paths to |1> cancel out:")
print(c, "\n")
with c.batch():          # rewire atomically, then cascade re-runs once
    c["flip"] = Z("a")   # insert a phase flip between them...
    c["b"] = H("flip")   # ...and point b at it
print("H, Z, H: now the paths to |0> cancel instead:")
print(c)

# 3 ─ Grover search: find the marked item among 4 in ONE query.
show("3. Grover search — finds |11> out of 4 options in one step")
g = Cascade(2)
g["s0"], g["s1"] = H("q0"), H("q1")
g["oracle"] = CZ("s0", "s1")                       # marks |11> with a minus sign
g["h0"], g["h1"] = H("oracle.0"), H("oracle.1")    # diffusion: amplify marked
g["x0"], g["x1"] = X("h0"), X("h1")
g["mirror"] = CZ("x0", "x1")
g["u0"], g["u1"] = X("mirror.0"), X("mirror.1")
g["out0"], g["out1"] = H("u0"), H("u1")
print(g)

# 4 ─ The compiler enforces physics: no copying a qubit.
show("4. No-cloning is a compile error")
bad = Cascade(2)
bad["a"] = H("q0")
try:
    bad["copy1"] = X("a")
    bad["copy2"] = X("a")
except NoCloningError as e:
    print("NoCloningError:", e)

# Optional: hand the same graph to real quantum tooling.
try:
    print("\nQiskit export of the Grover circuit:")
    print(g.to_qiskit())
except ImportError:
    print("(pip install qiskit to export to IBM's toolchain)")
