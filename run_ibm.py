"""
Run Cascade circuits on a real IBM quantum computer and save the results
for Cascade Listen's "Real" mode.

Setup (once):
    pip install numpy qiskit qiskit-ibm-runtime
    put your IBM Quantum API key in a file called ibm_key.txt next to this script

Run:
    python run_ibm.py            # all presets, one job on the least busy real machine
    python run_ibm.py --fake     # same thing on a noise model of ibm_kingston (no account needed;
                                 # also needs: pip install qiskit-aer)
    python run_ibm.py --only grover bell --shots 4000 --backend ibm_kingston
    python run_ibm.py --patch my_patch.json   # your own patch (Export > Patch file in Cascade Listen)

It writes results/<timestamp>-<backend>.json. Load that file in Cascade Listen
("Load results") to hear the real machine next to the ideal circuit.

Cost: each circuit takes a few seconds of machine time. IBM's free Open Plan
gives 10 minutes every 28 days, so a full run of all presets uses a small slice.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import sys

from cascade import Cascade, CCX, CX, CZ, H, RX, RY, RZ, S, SWAP, T, X, Y, Z

HERE = pathlib.Path(__file__).resolve().parent


# ------------------------------------------------------------- presets
# Same five patches as Cascade Listen, so the page can match them up.
def _dial(c):
    c["a"] = H("q0"); c["dial"] = RZ(0.5 * math.pi, "a"); c["out"] = H("dial")

def _bell(c):
    c["entangle"] = CX("spread", "q1"); c["spread"] = H("q0")

def _grover(c):
    c["s0"], c["s1"] = H("q0"), H("q1")
    c["oracle"] = CZ("s0", "s1")
    c["h0"], c["h1"] = H("oracle.0"), H("oracle.1")
    c["x0"], c["x1"] = X("h0"), X("h1")
    c["mirror"] = CZ("x0", "x1")
    c["u0"], c["u1"] = X("mirror.0"), X("mirror.1")
    c["out0"], c["out1"] = H("u0"), H("u1")

def _ghz(c):
    c["s"] = H("q0"); c["link1"] = CX("s", "q1"); c["link2"] = CX("link1.0", "q2")

def _rainbow(c):
    c["h0"], c["h1"], c["h2"] = H("q0"), H("q1"), H("q2")
    c["p0"], c["p1"], c["p2"] = S("h0"), T("h1"), RZ(0.25 * math.pi, "h2")

PRESETS = {  # name: (label, qubits, builder)
    "dial": ("Interference dial", 1, _dial),
    "bell": ("Bell pair (written backwards)", 2, _bell),
    "grover": ("Grover search", 2, _grover),
    "ghz": ("GHZ chord", 3, _ghz),
    "rainbow": ("Phase rainbow", 3, _rainbow),
}


GATE_FNS = {"H": H, "X": X, "Y": Y, "Z": Z, "S": S, "T": T, "RX": RX, "RY": RY, "RZ": RZ,
            "CX": CX, "CZ": CZ, "CCX": CCX, "SWAP": SWAP}


def add_patch_file(path):
    """Register a 'Patch file' exported from Cascade Listen as an extra preset."""
    d = json.loads(pathlib.Path(path).read_text())
    if d.get("format") != "cascade-patch/1":
        sys.exit(f"{path} isn't a patch file. In Cascade Listen use Export > Patch file.")

    def fn(c, nodes=d["patch"]):
        for p in nodes:
            g = GATE_FNS[p["gate"]]
            c[p["name"]] = g(p["theta"] * math.pi, *p["inputs"]) if "theta" in p else g(*p["inputs"])

    key = pathlib.Path(path).stem
    PRESETS[key] = (d.get("label") or key, int(d["n"]), fn)
    return key


def build(key):
    label, n, fn = PRESETS[key]
    c = Cascade(n)
    with c.batch():
        fn(c)
    return label, c


# ------------------------------------------------------------- helpers
def patch_of(c):
    """The circuit in Cascade Listen's patch format (θ in units of π)."""
    out = []
    for name, g in c.nodes.items():
        node = {"name": name, "gate": g.name, "inputs": list(g.inputs)}
        if g.params:
            node["theta"] = round(g.params[0] / math.pi, 6)
        out.append(node)
    return out


def load_key():
    if os.environ.get("QISKIT_IBM_TOKEN"):
        return os.environ["QISKIT_IBM_TOKEN"].strip()
    for p in (HERE / "ibm_key.txt", pathlib.Path.cwd() / "ibm_key.txt"):
        if p.exists():
            return p.read_text().strip()
    sys.exit("No API key found. Save your IBM Quantum API key in ibm_key.txt next to run_ibm.py.")


def get_backend(fake, name):
    if fake:
        from qiskit_aer import AerSimulator
        from qiskit_ibm_runtime.fake_provider import FakeKingston
        model = FakeKingston()
        return AerSimulator.from_backend(model), f"{model.name} (noise model)", "noise-model"
    from qiskit_ibm_runtime import QiskitRuntimeService
    service = QiskitRuntimeService(channel="ibm_quantum_platform", token=load_key())
    backend = service.backend(name) if name else service.least_busy(operational=True, simulator=False)
    return backend, backend.name, "hardware"


# ------------------------------------------------------------- run
def run(keys, shots=2000, fake=False, backend_name=None, record=512):
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
    from qiskit_ibm_runtime import SamplerV2  # legacy name, still current on IBM cloud

    backend, bname, kind = get_backend(fake, backend_name)
    print(f"Backend: {bname}")

    jobs = []
    for k in keys:
        label, c = build(k)
        qc = c.to_qiskit()
        qc.measure_all()
        jobs.append((k, label, c, qc))

    pm = generate_preset_pass_manager(backend=backend, optimization_level=3)
    isa = [pm.run(qc) for *_, qc in jobs]

    sampler = SamplerV2(mode=backend)
    job = sampler.run(isa, shots=shots)
    job_id = job.job_id() if callable(getattr(job, "job_id", None)) else None
    print(f"Submitted job {job_id or '(local)'} with {len(isa)} circuits x {shots} shots. Waiting...")
    result = job.result()

    runs = []
    for (k, label, c, _), pub in zip(jobs, result):
        # Qiskit writes bitstrings with qubit 0 on the right; Cascade puts q0 on the left.
        bits = [b[::-1] for b in pub.data.meas.get_bitstrings()]
        counts = {}
        for b in bits:
            counts[b] = counts.get(b, 0) + 1
        ideal = c.run().probabilities
        fid = sum(math.sqrt(ideal.get(b, 0) * counts.get(b, 0) / shots) for b in set(ideal) | set(counts)) ** 2
        runs.append({
            "preset": k, "label": label, "n": c.n, "shots": shots,
            "patch": patch_of(c),
            "counts": dict(sorted(counts.items())),
            "ideal": {b: round(p, 6) for b, p in sorted(ideal.items())},
            "record": bits[:record],  # the first shots in the order the machine produced them
            "fidelity": round(fid, 4),
        })
        print(f"  {label:<30} match with ideal {fid:6.1%}   {dict(sorted(counts.items()))}")

    out = {
        "format": "cascade-results/1",
        "backend": bname, "kind": kind, "job_id": job_id,
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "runs": runs,
    }
    folder = HERE / "results"
    folder.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    path = folder / f"{stamp}-{bname.split(' ')[0]}{'-model' if fake else ''}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"Saved {path}")
    return path


# ------------------------------------------------------------- compare
def _bits(pub):
    # Qiskit writes bitstrings with qubit 0 on the right; Cascade puts q0 on the left.
    return [b[::-1] for b in pub.data.meas.get_bitstrings()]


def _probs(bits):
    out = {}
    for b in bits:
        out[b] = out.get(b, 0) + 1
    return {b: c / len(bits) for b, c in sorted(out.items())}


def run_compare(keys, shots=4000, fake=False, backend_name=None, record=512):
    """Run each circuit twice, side by side: IBM's default compile vs. Cascade's
    noise-fighting compiler (placement + readout correction + decoupling)."""
    import mitigate as M
    from qiskit_ibm_runtime import SamplerV2

    backend, bname, kind = get_backend(fake, backend_name)
    print(f"Backend: {bname}\nCompiling each circuit {24 + 1} ways and scoring them on today's calibration data...")

    items = []
    for k in keys:
        label, c = build(k)
        qc = c.to_qiskit(); qc.measure_all()
        default, d_score, best, b_score = M.best_transpile(qc, backend)
        phys = M.physical_qubits(best, c.n)
        items.append(dict(k=k, label=label, c=c, default=default, best=best, d_score=d_score,
                          b_score=b_score, phys=phys, cal=M.calibration_circuits(phys, backend)))
        print(f"  {label:<30} predicted success {d_score:6.1%} -> {b_score:6.1%}  on qubits {phys}")

    base_job = SamplerV2(mode=backend).run([it["default"] for it in items], shots=shots)
    smart = SamplerV2(mode=backend)
    if kind == "hardware":
        smart.options.dynamical_decoupling.enable = True
        smart.options.dynamical_decoupling.sequence_type = "XpXm"
    smart_pubs = []
    for it in items:
        smart_pubs += [it["best"], *it["cal"]]
    smart_job = smart.run(smart_pubs, shots=shots)
    ids = [j.job_id() if callable(getattr(j, "job_id", None)) else None for j in (base_job, smart_job)]
    print(f"Submitted jobs {ids[0] or '(local)'} (default) and {ids[1] or '(local)'} (cascade). Waiting...")
    base_res, smart_res = base_job.result(), smart_job.result()

    runs = []
    print()
    for i, it in enumerate(items):
        c, label = it["c"], it["label"]
        ideal = c.run().probabilities
        common = {"preset": it["k"], "n": c.n, "shots": shots, "patch": patch_of(c),
                  "ideal": {b: round(p, 6) for b, p in sorted(ideal.items())}}

        b_bits = _bits(base_res[i])
        b_probs = _probs(b_bits)
        b_fid = M.fidelity(ideal, b_probs)

        s_bits = _bits(smart_res[3 * i])
        e0, e1 = M.flip_rates(_bits(smart_res[3 * i + 1]), _bits(smart_res[3 * i + 2]), c.n)
        s_raw = _probs(s_bits)
        s_probs = M.correct_readout(s_raw, e0, e1)
        place_fid, s_fid = M.fidelity(ideal, s_raw), M.fidelity(ideal, s_probs)

        runs.append({**common, "label": f"{label} · IBM default", "variant": "default",
                     "counts": {b: round(p * shots, 3) for b, p in b_probs.items()},
                     "record": b_bits[:record], "fidelity": round(b_fid, 4),
                     "predicted": round(it["d_score"], 4)})
        runs.append({**common, "label": f"{label} · Cascade compiler", "variant": "cascade", "mitigated": True,
                     "counts": {b: round(p * shots, 3) for b, p in s_probs.items()},
                     "raw_counts": {b: round(p * shots) for b, p in s_raw.items()},
                     "record": None, "fidelity": round(s_fid, 4), "fidelity_before_readout_fix": round(place_fid, 4),
                     "predicted": round(it["b_score"], 4), "physical_qubits": it["phys"],
                     "readout_flip_rates": {"read_1_when_0": [round(x, 4) for x in e0],
                                            "read_0_when_1": [round(x, 4) for x in e1]},
                     "baseline_fidelity": round(b_fid, 4)})
        print(f"  {label:<30} IBM default {b_fid:6.1%}   Cascade {place_fid:6.1%} placed"
              f"  -> {s_fid:6.1%} with readout fix   ({(s_fid - b_fid) * 100:+.1f} pts)")

    out = {"format": "cascade-results/1", "backend": bname, "kind": kind, "job_id": ",".join(i or "" for i in ids),
           "comparison": True, "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "runs": runs}
    folder = HERE / "results"; folder.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    path = folder / f"{stamp}-{bname.split(' ')[0]}-compare{'-model' if fake else ''}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"\nSaved {path}")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compare", action="store_true",
                    help="run IBM's default compile and Cascade's noise-fighting compiler side by side")
    ap.add_argument("--only", nargs="+", choices=list(PRESETS), help="run only these presets")
    ap.add_argument("--shots", type=int, default=2000)
    ap.add_argument("--backend", help="a specific machine, e.g. ibm_kingston (default: least busy)")
    ap.add_argument("--fake", action="store_true", help="use a noise model instead of real hardware")
    ap.add_argument("--patch", nargs="+", metavar="FILE", help="run patch files exported from Cascade Listen")
    a = ap.parse_args()
    if a.compare:
        keys = [add_patch_file(f) for f in a.patch] if a.patch else (a.only or ["bell", "grover", "ghz"])
        run_compare(keys, shots=a.shots if a.shots != 2000 else 4000, fake=a.fake, backend_name=a.backend)
    else:
        keys = [add_patch_file(f) for f in a.patch] if a.patch else (a.only or list(PRESETS))
        run(keys, shots=a.shots, fake=a.fake, backend_name=a.backend)
