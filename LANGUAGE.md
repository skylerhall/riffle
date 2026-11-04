# The Riffle language

Riffle is a small language for quantum circuits. A program is a set of named values that flow
from gate to gate, like water through a riffle. You say what each value is made from, in any
order, and the compiler works out the order, checks the physics, and runs it.

Files end in `.rfl`.

```riffle
// Grover search over 2 qubits: finds the marked answer |11> in one step.
circuit grover(q0, q1) {
    s0 = H q0                  // start with every answer equally likely
    s1 = H q1
    o0, o1 = CZ s0, s1         // oracle: flip the sign of |11>
    h0 = H o0                  // diffusion: amplify the marked answer
    h1 = H o1
    x0 = X h0
    x1 = X h1
    m0, m1 = CZ x0, x1
    u0 = X m0
    u1 = X m1
    out0 = H u0
    out1 = H u1
}
```

```
$ riffle run examples/grover.rfl
circuit grover: 12 gates on 2 qubit(s) (q0, q1)
  |11>  100.0%  ██████████████████████████████
```

## Circuits

```riffle
circuit name(qubit, qubit, ...) work(scratch, ...) {
    ...
}
```

- The names in parentheses are the circuit's qubits. Each starts at |0⟩. Call them anything.
- `work(...)` is optional and declares [work qubits](#work-qubits-and-automatic-cleanup).
- A file can hold several circuits. Tools use the first one unless you name another (`-c name`).

## Statements

Every statement names the value a gate produces:

```riffle
a = H q0                 // a is q0 after a Hadamard
c, t = CX a, q1          // a two-qubit gate gives two values: control and target
both = CCX a, b, w       // or name just one and use both.0, both.1, both.2
turn = RZ(pi/2) a        // rotations take an angle
```

- **One gate per line.** Separate several on one line with `;` if you like.
- **Order doesn't matter.** A value can be used above the line that defines it. The compiler
  sorts the graph, so circuits can be written in whatever order reads best.
- **Every name is defined once.** A name holds one value forever; give the next value a new name.
- **Every value is used at most once.** A qubit's state can't be copied (the no-cloning
  theorem), so feeding one value into two gates is a compile error. A value nobody uses is
  simply the circuit's output on that qubit line.
- Output *i* of a gate continues input *i*'s qubit line: in `c, t = CX a, q1`, `c` is the
  control qubit afterwards and `t` the target.

## Gates

| Gate | Inputs | What it does |
|---|---|---|
| `H` | 1 | Hadamard: splits a qubit into an even superposition |
| `X` `Y` `Z` | 1 | Pauli flips (`Z` flips the phase of the 1 part) |
| `S` `T` | 1 | Quarter- and eighth-turn phase |
| `RX(θ)` `RY(θ)` `RZ(θ)` | 1 | Rotation by an angle |
| `CX` (or `CNOT`) | 2 | Controlled NOT: flips the target when the control is 1 |
| `CZ` | 2 | Controlled phase flip |
| `SWAP` | 2 | Exchanges two qubits |
| `CCX` (or `TOFFOLI`) | 3 | NOT controlled by two qubits |

Gate names aren't case sensitive. **Angles** are written with numbers and `pi` (or `π`):
`pi/2`, `0.25*pi`, `3pi/4`, `-pi/8`, or plain radians like `1.2`.

## Work qubits and automatic cleanup

Many algorithms need scratch space: compute something into a spare qubit, use it, then put it
back to |0⟩. Forget the last step and the leftover value quietly ruins the interference the
algorithm depends on. Riffle does that last step for you.

```riffle
circuit grover_work(q0, q1) work(scratch) {
    s0 = H q0
    s1 = H q1
    a0, a1, both = CCX s0, s1, scratch    // scratch = q0 AND q1
    marked       = Z both                 // use it; Riffle adds the undo right after this
    ...
}
```

The compiler inserts the undo right after the work qubit's last use, wired in before anything
changes the qubits it was computed from. The rules that make this safe are checked at compile time:

- a work qubit may only be **written** by `X`, `CX` or `CCX` (as the target),
- and after that only **read**: as a control, or by `Z`, `S`, `T`, `RZ`, `CZ`,
- and the qubits it was computed from can't change before its last read.

Breaking a rule is a compile error that says which rule and where. Write `cleanup off` inside a
circuit to see what happens without cleanup (Grover's answer drops from 100% to 62.5%).

## Comments

`//` or `#` to the end of the line.

## Errors

Mistakes are reported with the file, line and a plain-English fix:

```
error: grover.rfl:line 4: 'a' is used twice: by line 3 and by this line. A qubit's state can't be copied, so each value can be used only once
error: grover.rfl:line 3: 'aa' isn't defined anywhere in circuit 'c'. Did you mean 'a'?
error: grover.rfl:line 2: 'HH' isn't a gate. Did you mean H?
error: grover.rfl:line 3: these gates feed each other in a loop: 'a', 'b'
```

## The `riffle` command

```bash
riffle run file.rfl                 # simulate and print the probabilities (--shots N to sample)
riffle check file.rfl               # check for mistakes
riffle fmt file.rfl                 # reprint with aligned columns (-w to rewrite the file)
riffle build file.rfl --qasm        # OpenQASM 3: IBM Quantum and most other hardware
riffle build file.rfl --guppy       # Guppy: Quantinuum's language
riffle build file.rfl --hugr out.hugr   # a HUGR package for Quantinuum's stack
riffle build file.rfl --python      # the same circuit as Python (riffle.py)
```

Install with `pip install -e .` from this folder, or run `python riffle_lang.py ...` directly.

## Grammar

```
file       = circuit*
circuit    = "circuit" NAME "(" names ")" [ "work" "(" names ")" ] "{" statement* "}"
statement  = names "=" GATE [ "(" angle ")" ] ref ("," ref)*
           | "cleanup" ("on" | "off")
names      = NAME ("," NAME)*
ref        = NAME [ "." INTEGER ]
angle      = ["-"] factor ( ("*" | "/") factor | factor )*
factor     = NUMBER | "pi" | "π"
```

Statements end at a newline, `;` or `}`. Reserved words: `circuit`, `work`, `cleanup`, `on`,
`off`, `pi`. Names that look like built-in qubits (`q0`, `w3`) are only allowed as header qubits.

## Not yet in the language

Reusable sub-circuits (calling one circuit from another), loops, and mid-circuit measurement
with classical control. HUGR, Riffle's Quantinuum target, supports all three, so they can be
added without changing how programs run.
