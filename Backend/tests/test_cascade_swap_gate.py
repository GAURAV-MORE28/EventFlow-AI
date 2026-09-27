"""ML Phase 4: the swap gate (03 §4.3, §4.4, §4.5) for the configured cascade bundle.

The evidence lives beside the bundle: its eval.json (held-out maps) and, in
ML/evaluation/results/, the shadow run and the reproduction written by
`python -m scripts.cascade_swap_eval shadow|reproduce`. Evidence for another
model version (a retrained bundle) does not count: rerun the script."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from app.config import get_config

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

pytest.importorskip("torch")
from ML.evaluation import cascade_eval as ev  # noqa: E402
from ML.manifest import load_manifest  # noqa: E402

RESULTS = REPO / "ML" / "evaluation" / "results"


def _gate(bundle_dir: Path | None = None) -> dict:
    raw = get_config().raw
    cascade = raw["cascade"]
    bundle = bundle_dir or REPO / cascade["gnn_artifact"]
    manifest = load_manifest(bundle)   # verifies every file's hash
    sha = manifest["files"]["model.pt"]["sha256"]
    version = f"{manifest['model_version']}@{sha[:8]}"
    read = lambda p: json.loads(p.read_text(encoding="utf-8")) if p.exists() else None  # noqa: E731
    return ev.swap_gate(
        json.loads((bundle / "eval.json").read_text(encoding="utf-8")),
        read(RESULTS / f"{manifest['model_version']}_shadow.json"),
        read(RESULTS / f"{manifest['model_version']}_reproduce.json"),
        version, cascade["swap_gate"], float(raw["budgets_ms"]["cascade"]))


def test_configured_bundle_passes_the_swap_gate():
    gate = _gate()
    assert gate["passes"], f"swap gate failed for {gate['model_version']}: {gate['failed']}"


def test_annotate_mode_requires_a_passing_gate():
    """What the gate protects: published cascades carry model probabilities only
    for a model that earned it."""
    if get_config().raw["cascade"].get("gnn_mode") == "annotate":
        gate = _gate()
        assert gate["passes"], f"gnn_mode is annotate but the gate fails: {gate['failed']}"


def test_v2_does_not_pass_the_gate():
    """The gate discriminates: v2 (miscalibrated, below the precision floor on
    held-out maps, no shadow evidence) must fail it."""
    gate = _gate(REPO / "ML" / "artifacts" / "hx_cascade_v2")
    assert not gate["passes"]
    assert "shadow.present_for_this_model" in gate["failed"]


def test_gate_rejects_evidence_for_another_model_version():
    raw = get_config().raw
    bundle = REPO / raw["cascade"]["gnn_artifact"]
    name = load_manifest(bundle)["model_version"]
    shadow = json.loads((RESULTS / f"{name}_shadow.json").read_text(encoding="utf-8"))
    gate = ev.swap_gate(json.loads((bundle / "eval.json").read_text(encoding="utf-8")), shadow, None,
                        f"{name}@00000000", raw["cascade"]["swap_gate"], 150.0)
    assert not gate["passes"]
    assert {"shadow.present_for_this_model", "reproduce.present_for_this_model"} <= set(gate["failed"])


def test_gate_thresholds_are_enforced():
    """Tightening any limit past the evidence flips the verdict."""
    raw = get_config().raw
    bundle = REPO / raw["cascade"]["gnn_artifact"]
    manifest = load_manifest(bundle)
    version = f"{manifest['model_version']}@{manifest['files']['model.pt']['sha256'][:8]}"
    ev_json = json.loads((bundle / "eval.json").read_text(encoding="utf-8"))
    read = lambda k: json.loads((RESULTS / f"{manifest['model_version']}_{k}.json").read_text(encoding="utf-8"))  # noqa: E731
    base = dict(raw["cascade"]["swap_gate"])
    for key, value in (("min_precision", 0.99), ("min_recall", 0.99), ("max_ece", 0.0001)):
        gate = ev.swap_gate(ev_json, read("shadow"), read("reproduce"), version, {**base, key: value}, 150.0)
        assert not gate["passes"], key


def test_shadow_run_is_deterministic_under_hash_randomisation():
    """The twin draws observation noise in its own entity order, so a run does
    not depend on PYTHONHASHSEED (it did: dict order came from set iteration)."""
    import subprocess

    code = ("import os,asyncio,json;os.environ['DATABASE_URL']='sqlite://';"
            "from scripts.cascade_dataset import HeadlessEngine,GeneratedCity,_unbounded_budgets;"
            "from app.config import get_config;from app.topology import build_topology;"
            "_unbounded_budgets();"
            "e=HeadlessEngine(GeneratedCity(build_topology(),get_config().raw['events']),42);e.prime_state()\n"
            "async def go():\n    for _ in range(12): await e.run_cycle()\n"
            "asyncio.run(go())\n"
            "print(json.dumps({k:v['utilisation'] for k,v in sorted(e.store.entity_states.items())}))")
    outs = []
    for seed in ("0", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run([sys.executable, "-c", code], cwd=REPO / "Backend", env=env, capture_output=True,
                             text=True, timeout=300)
        assert run.returncode == 0, run.stderr[-2000:]
        outs.append(run.stdout.strip().splitlines()[-1])
    assert outs[0] == outs[1]
