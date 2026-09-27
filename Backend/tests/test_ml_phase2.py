"""ML Phase 2: the v3 training pipeline — random maps, one feature builder for
training and serving, the v3 network, calibration and the evaluation harness."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from app.config import get_config
from app.topology import build_topology

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

torch = pytest.importorskip("torch")
pytest.importorskip("torch_geometric")
from ML.data.topology_randomiser import problems, randomise  # noqa: E402
from ML.evaluation import cascade_eval as ev  # noqa: E402
from ML.features import graph_features as gf  # noqa: E402
from ML.manifest import topology_hash  # noqa: E402
from ML.models.hx_cascade import HXCascadeV3, build_model  # noqa: E402
from ML.training.train_v3 import (_configs, _pos_weight, calibrated, choose_thresholds,  # noqa: E402
                                  fit_temperatures)


def _base():
    cfg = get_config().raw
    return build_topology(), cfg["events"], cfg["event"]["event_id"]


# --- topology randomiser ----------------------------------------------------------------------
def test_randomised_maps_are_valid_deterministic_and_distinct():
    topo, events, primary = _base()
    live = topology_hash(topo["nodes"], topo["edges"])
    maps = [randomise(topo, events, seed=s, primary_event_id=primary) for s in range(12)]
    for m in maps:
        assert problems(m["topology"], m["events"]) == []
        assert m["topology_hash"] == topology_hash(m["topology"]["nodes"], m["topology"]["edges"])
        assert m["topology_hash"] != live
    assert randomise(topo, events, seed=3, primary_event_id=primary)["topology_hash"] == maps[3]["topology_hash"]
    assert len({m["topology_hash"] for m in maps}) == len(maps)
    # Structure changes, not only numbers: some draw adds and some removes each kind.
    ops = [op.split(":")[0] for m in maps for op in m["ops"]]
    for kind in ("add_gate", "remove_gate", "add_road", "remove_road", "add_zone", "remove_zone"):
        assert kind in ops, kind


def test_randomiser_keeps_the_primary_event_and_moves_schedules():
    topo, events, primary = _base()
    m = randomise(topo, events, seed=5, primary_event_id=primary)
    assert primary in {e["event_id"] for e in m["events"]}
    before = {e["event_id"]: (e["start_time"], e["expected_attendance"]) for e in events}
    moved = [e for e in m["events"] if (e["start_time"], e["expected_attendance"]) != before[e["event_id"]]]
    assert moved


def test_problems_detects_broken_maps():
    topo, events, _ = _base()
    venue = events[0]["venue_entity_id"]
    cut = {**topo, "edges": [e for e in topo["edges"] if venue not in (e["src_entity_id"], e["dst_entity_id"])]}
    found = problems(cut, events)
    assert any("unreachable" in p or "not connected" in p for p in found)
    dangling = {**topo, "edges": topo["edges"] + [{**topo["edges"][0], "edge_id": "x", "dst_entity_id": "nowhere"}]}
    assert "dangling edges" in problems(dangling, events)


# --- one feature builder ----------------------------------------------------------------------
def _node_state(topo: dict, rng: np.random.Generator) -> dict[str, dict]:
    out = {}
    for n in topo["nodes"]:
        u = float(rng.uniform(0.1, 1.1))
        out[n["entity_id"]] = {
            "entity_id": n["entity_id"], "entity_type": n["entity_type"], "nominal_capacity": n["nominal_capacity"],
            "utilisation": u, "forecast_900": u + 0.05, "forecast_1800": None, "forecast_3600": u + 0.2,
            "is_observed": bool(rng.random() < 0.5), "flow_rate_per_min": float(rng.normal(0, 20)),
        }
    return out


def test_training_and_serving_build_identical_features():
    """The dataset stores the published arrays; training rebuilds features from
    them with the same functions serving calls on `node_state_for_ml()`."""
    topo, _, _ = _base()
    ns = _node_state(topo, np.random.default_rng(0))
    thresholds = get_config().raw["thresholds"]
    served = gf.build(ns, topo["edges"], thresholds)

    ids = list(ns)
    types = [ns[e]["entity_type"] for e in ids]
    nan = lambda v: np.nan if v is None else v  # noqa: E731
    graph = gf.static_graph(ids, types, topo["edges"])
    trained = gf.node_features(
        np.array([ns[e]["utilisation"] for e in ids], dtype=np.float32),
        np.array([[nan(ns[e][f"forecast_{h}"]) for h in gf.HORIZONS] for e in ids], dtype=np.float32),
        np.array([ns[e]["nominal_capacity"] for e in ids], dtype=np.float32), types,
        gf.critical_lines(types, thresholds),
        np.array([ns[e]["is_observed"] for e in ids], dtype=bool),
        np.array([ns[e]["flow_rate_per_min"] for e in ids], dtype=np.float32), graph)
    np.testing.assert_array_equal(served["x"], trained)
    np.testing.assert_array_equal(served["edge_attr"], graph["edge_attr"])
    assert served["x"].shape == (len(ids), gf.NODE_FEAT_DIM)
    assert np.isfinite(served["x"]).all()


def test_critical_lines_come_from_config_thresholds():
    thresholds = get_config().raw["thresholds"]
    lines = gf.critical_lines(["gate", "venue", "hotel"], thresholds)
    assert lines[0] == pytest.approx(thresholds["critical_utilisation"])
    assert lines[1] == pytest.approx(thresholds["by_type"]["venue"]["critical"])
    assert lines[2] == pytest.approx(thresholds["by_type"]["hotel"]["critical"])


def test_edge_attributes_carry_coefficient_travel_time_and_direction():
    topo, _, _ = _base()
    ids = [n["entity_id"] for n in topo["nodes"]]
    g = gf.static_graph(ids, [n["entity_type"] for n in topo["nodes"]], topo["edges"])
    assert g["edge_index"].shape[1] == 2 * len(topo["edges"])
    col = {name: i for i, name in enumerate(gf.EDGE_FEATURE_ORDER)}
    assert g["edge_attr"][:, col["is_reverse"]].sum() == len(topo["edges"])
    assert g["edge_attr"][:, col["transfer_coefficient"]].max() > 0
    assert g["edge_attr"][:, col["travel_time"]].max() > 0


# --- v3 network -------------------------------------------------------------------------------
def test_v3_probabilities_are_monotone_across_horizons_and_ttc_bounded():
    torch.manual_seed(0)
    model = build_model({"class": "HXCascadeV3", "node_feat_dim": gf.NODE_FEAT_DIM, "edge_feat_dim": gf.EDGE_FEAT_DIM})
    assert isinstance(model, HXCascadeV3)
    model.eval()
    x = torch.randn(30, gf.NODE_FEAT_DIM) * 3
    ei = torch.randint(0, 30, (2, 80))
    ea = torch.rand(80, gf.EDGE_FEAT_DIM)
    with torch.no_grad():
        out = model(x, ei, ea)
    p = torch.sigmoid(out["failure_logits"])
    assert torch.all(p[:, 1] >= p[:, 0] - 1e-6) and torch.all(p[:, 2] >= p[:, 1] - 1e-6)
    assert torch.all((out["ttc"] >= 0) & (out["ttc"] <= 3600))
    assert out["embedding"].shape == (30, 64)


# --- calibration and thresholds ---------------------------------------------------------------
def test_temperature_scaling_recovers_an_overconfident_model():
    rng = np.random.default_rng(1)
    true_logit = rng.normal(0, 1.5, size=(20000, 3))
    labels = rng.random((20000, 3)) < 1 / (1 + np.exp(-true_logit))
    temps = fit_temperatures(true_logit * 3.0, labels)   # logits three times too sharp
    assert all(t == pytest.approx(3.0, rel=0.1) for t in temps)
    p = calibrated(true_logit * 3.0, temps)
    assert np.all(np.diff(p, axis=-1) >= 0)
    assert ev.expected_calibration_error(p[:, 0], labels[:, 0].astype(float)) < 0.02
    th = choose_thresholds(p, labels)
    assert all(0.05 <= t <= 0.95 for t in th)


# --- evaluation harness -----------------------------------------------------------------------
def test_average_precision_and_ece_edge_cases():
    y = np.array([1, 0, 1, 0, 0], dtype=bool)
    assert ev.average_precision(np.array([0.9, 0.1, 0.8, 0.2, 0.3]), y) == pytest.approx(1.0)
    assert ev.average_precision(np.zeros(5), np.zeros(5, dtype=bool)) != ev.average_precision(np.zeros(5), np.zeros(5, dtype=bool))  # NaN
    assert ev.expected_calibration_error(np.array([0.0, 1.0]), np.array([0.0, 1.0])) == pytest.approx(0.0)


def _fake_run(cluster: str, rng: np.random.Generator, S: int = 6, N: int = 5) -> ev.Run:
    ttc = np.where(rng.random((S, N)) < 0.4, rng.integers(1, 120, (S, N)) * 30.0, np.nan).astype(np.float32)
    labels = np.stack([np.nan_to_num(ttc, nan=1e9) <= h for h in ev.HORIZONS], axis=-1)
    steps = np.arange(20, 20 + 10 * S, 10)
    crossings = np.array([[steps[s] + int(ttc[s, e] // 30), e] for s in range(S) for e in range(N)
                          if not np.isnan(ttc[s, e])], dtype=np.int32).reshape(-1, 2)
    good = {"scores": labels.astype(np.float32) * 0.8 + 0.1, "alerts": labels.copy(), "ttc": ttc.copy()}
    none = {"scores": np.zeros((S, N, 3), np.float32), "alerts": np.zeros((S, N, 3), bool),
            "ttc": np.full((S, N), np.nan, np.float32)}
    return ev.Run(cluster=cluster, mask=np.ones((S, N), bool), labels=labels, ttc_true=ttc, snapshot_step=steps,
                  crossings=crossings, cycle_sec=30, predictions={"oracle": good, "silent": none})


def test_evaluate_reports_intervals_over_map_clusters():
    rng = np.random.default_rng(2)
    runs = [_fake_run(f"map_{i}", rng) for i in range(6)]
    oracle = ev.evaluate(runs, "oracle", probabilistic=True, bootstrap=50, seed=0)
    assert oracle["clusters"] == 6
    assert oracle["point"]["3600"]["precision"] == 1.0 and oracle["point"]["3600"]["recall"] == 1.0
    assert oracle["point"]["ttc_mae_sec"] == 0.0
    assert oracle["point"]["event_recall"] == 1.0 and oracle["point"]["lead_time_sec"] > 0
    assert "3600.average_precision" in oracle["ci95"]
    silent = ev.evaluate(runs, "silent", probabilistic=False, bootstrap=50, seed=0)
    assert silent["point"]["3600"]["recall"] == 0.0 and silent["point"]["3600"]["precision"] is None
    one_map = ev.evaluate(runs[:1], "oracle", probabilistic=True, bootstrap=50)
    assert one_map["ci95"] == {} and one_map["ci_note"]


def test_sample_mask_excludes_scored_entities_already_over_their_line():
    npz = {"types": np.array(["gate", "gate", "hotel"]), "util": np.array([[0.5, 0.95, 0.1]], np.float32),
           "critical": np.array([0.9, 0.9, 0.98], np.float32)}
    assert ev.sample_mask(npz, ("gate",)).tolist() == [[True, False, False]]


def test_baselines_come_from_recorded_arrays():
    S, N = 2, 3
    npz = {"critical": np.full(N, 0.9, np.float32),
           "det_eta": np.array([[600, np.nan, 2400], [np.nan, np.nan, np.nan]], np.float32),
           "det_score": np.array([[0.7, 0.2, 0.4], [0, 0, 0]], np.float32),
           "forecasts": np.full((S, N, 3), 0.5, np.float32),
           "fc_ttc": np.array([[np.nan, 1200, np.nan], [np.nan] * 3], np.float32),
           "v2_p": np.full((S, N, 3), 0.7, np.float32), "v2_ttc": np.zeros((S, N), np.float32)}
    b = ev.baseline_predictions(npz, v2_threshold=0.6)
    assert set(b) == {"deterministic_cascade", "forecast_rule", "hx_cascade_v2"}
    assert b["deterministic_cascade"]["alerts"][0, 0].tolist() == [True, True, True]
    assert b["deterministic_cascade"]["alerts"][0, 2].tolist() == [False, False, True]
    assert b["forecast_rule"]["alerts"][0, 1].tolist() == [False, True, True]
    assert b["hx_cascade_v2"]["alerts"].all()
    npz["v2_p"][:] = np.nan
    assert "hx_cascade_v2" not in ev.baseline_predictions(npz, 0.6)


# --- sweep ------------------------------------------------------------------------------------
def test_sweep_entries_override_the_base_config_and_unknown_keys_are_refused():
    import argparse

    args = argparse.Namespace(hidden=64, layers=3, heads=4, dropout=0.1, lr=2e-3, batch_size=32,
                              pos_weight="sqrt", sweep=None)
    assert _configs(args) == [{"hidden": 64, "layers": 3, "heads": 4, "dropout": 0.1, "lr": 2e-3,
                               "batch_size": 32, "pos_weight": "sqrt"}]
    args.sweep = '[{}, {"hidden": 128, "pos_weight": "none"}]'
    configs = _configs(args)
    assert [c["hidden"] for c in configs] == [64, 128] and configs[1]["pos_weight"] == "none"
    assert configs[1]["layers"] == 3
    args.sweep = '[{"hiden": 128}]'
    with pytest.raises(SystemExit):
        _configs(args)


def test_class_weighting_modes():
    from torch_geometric.data import Data

    y = torch.zeros(100, 3)
    y[:10] = 1.0
    g = [Data(y=y, mask=torch.ones(100, dtype=torch.bool))]
    assert torch.allclose(_pos_weight(g, "none"), torch.ones(3))
    assert torch.allclose(_pos_weight(g, "linear"), torch.full((3,), 9.0))
    assert torch.allclose(_pos_weight(g, "sqrt"), torch.full((3,), 3.0))
    with pytest.raises(ValueError):
        _pos_weight(g, "cubic")
