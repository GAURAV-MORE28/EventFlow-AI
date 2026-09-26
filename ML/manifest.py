"""Model artifact bundles: one directory per model version with a manifest that
binds every file by SHA-256 (standard library only, so both the inference code
and the training scripts can use it without importing anything backend-shaped).

    ML/artifacts/<version>/
        manifest.json        this file's schema (below)
        model.pt             weights
        feature_norm.json    feature layout the inference code reads
        eval.json            held-out evaluation behind this version (optional)
        calibration.json     per-horizon temperatures (optional; absent = uncalibrated)

manifest.json:
    {
      "model_name": "hx_cascade",
      "model_version": "hx_cascade_v2",
      "architecture": {"class": "HXCascade", "node_feat_dim": 15, "hidden": 64,
                       "num_layers": 2, "num_edge_types": 6},
      "files": {"model.pt": {"sha256": "..."}, ...},
      "topology_hash": "..." | null,     # topology the model was trained on
      "calibrated": false,
      "evaluated_outputs": [...],        # which heads the eval report covers
      "trained_on": "...", "created_by": "...", "source_commit": "..."
    }

A bundle whose files do not match their hashes is not loaded (the caller falls
back); there is no filename guessing between checkpoints and norm files.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"


class ManifestError(Exception):
    """The bundle is missing, incomplete, or a file does not match its hash."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(bundle_dir: Path) -> dict[str, Any]:
    """Read and verify a bundle. Raises ManifestError on any mismatch."""
    bundle_dir = Path(bundle_dir)
    path = bundle_dir / MANIFEST
    if not path.exists():
        raise ManifestError(f"no {MANIFEST} in {bundle_dir}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest.get("files") or {}
    for required in ("model.pt", "feature_norm.json"):
        if required not in files:
            raise ManifestError(f"{MANIFEST} does not list {required}")
    for name, meta in files.items():
        f = bundle_dir / name
        if not f.exists():
            raise ManifestError(f"{name} listed in {MANIFEST} is missing")
        actual = sha256_file(f)
        if actual != meta.get("sha256"):
            raise ManifestError(f"{name} sha256 {actual[:12]} does not match manifest {str(meta.get('sha256'))[:12]}")
    return manifest


def write_manifest(bundle_dir: Path, **fields: Any) -> dict[str, Any]:
    """Hash every file in the bundle (except the manifest) and write the manifest."""
    bundle_dir = Path(bundle_dir)
    files = {p.name: {"sha256": sha256_file(p)} for p in sorted(bundle_dir.iterdir())
             if p.is_file() and p.name != MANIFEST}
    manifest = {**fields, "files": files}
    (bundle_dir / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def topology_hash(nodes: list[dict] | dict[str, dict], edges: list[dict]) -> str:
    """Stable hash of what the model's features depend on: entity ids, types and
    capacities, and edge endpoints, types, coefficients and travel times.

    `nodes` may be a list of Entity dicts or the `node_state_for_ml()` mapping."""
    rows = nodes.values() if isinstance(nodes, dict) else nodes
    node_part = sorted(
        (str(n["entity_id"]), str(n["entity_type"]), round(float(n.get("nominal_capacity") or 0.0), 6))
        for n in rows
    )
    edge_part = sorted(
        (str(e["src_entity_id"]), str(e["dst_entity_id"]), str(e["edge_type"]),
         round(float(e.get("transfer_coefficient") or 0.0), 6), int(e.get("travel_time_sec") or 0))
        for e in edges
    )
    blob = json.dumps({"nodes": node_part, "edges": edge_part}, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
