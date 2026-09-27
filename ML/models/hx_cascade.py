"""HX-Cascade network definitions.

`HXCascade`   — v2: R-GCN over relation types only (no edge attributes); kept so
                the v2 bundle keeps loading.
`HXCascadeV3` — v3: message passing that reads edge attributes (edge type,
                transfer coefficient, travel time, substitutability, direction),
                failure probabilities that are monotone across horizons by
                construction, and a time-to-critical head trained and evaluated
                on the entities that do cross.

Both return {"failure_logits": (N, 3) logit of P(cross within 900/1800/3600 s),
             "ttc": (N,) seconds}. v3 also returns "embedding" (N, hidden) for the
out-of-distribution statistics.
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import RGCNConv, TransformerConv

TTC_MAX_SEC = 3600.0


class HXCascade(nn.Module):
    """v2. Must match the trained checkpoint's shapes exactly — never modify this
    to "fit" a feature mismatch; fix the feature-building code instead."""

    def __init__(self, node_feat_dim: int = 12, hidden: int = 64, num_layers: int = 2, num_edge_types: int = 6):
        super().__init__()
        self.input_proj = nn.Linear(node_feat_dim, hidden)
        self.convs = nn.ModuleList(
            [RGCNConv(hidden, hidden, num_relations=num_edge_types) for _ in range(num_layers)]
        )
        self.norm = nn.ModuleList([nn.LayerNorm(hidden) for _ in range(num_layers)])
        self.head_failure = nn.Linear(hidden, 3)  # logits for [900s, 1800s, 3600s]
        self.head_ttc = nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, edge_type: torch.Tensor) -> dict[str, torch.Tensor]:
        h = torch.relu(self.input_proj(x))
        for conv, norm in zip(self.convs, self.norm):
            h = torch.relu(norm(conv(h, edge_index, edge_type))) + h
        return {
            "failure_logits": self.head_failure(h),
            "ttc": F.relu(self.head_ttc(h)).squeeze(-1),
        }


class HXCascadeV3(nn.Module):
    """Edge-attributed graph transformer with a discrete-time hazard head.

    The three failure outputs are per-interval hazards q1 (0-900 s), q2
    (900-1800 s), q3 (1800-3600 s); P(cross by h) = 1 - prod(1 - q), so
    p_900 <= p_1800 <= p_3600 always holds.
    """

    def __init__(self, node_feat_dim: int, edge_feat_dim: int, hidden: int = 64, num_layers: int = 3,
                 heads: int = 4, dropout: float = 0.1):
        super().__init__()
        if hidden % heads:
            raise ValueError("hidden must be divisible by heads")
        self.input_proj = nn.Linear(node_feat_dim, hidden)
        self.convs = nn.ModuleList([
            TransformerConv(hidden, hidden // heads, heads=heads, edge_dim=edge_feat_dim, dropout=dropout)
            for _ in range(num_layers)
        ])
        self.norms = nn.ModuleList([nn.LayerNorm(hidden) for _ in range(num_layers)])
        self.head_hazard = nn.Sequential(nn.Linear(hidden, hidden), nn.ReLU(), nn.Linear(hidden, 3))
        self.head_ttc = nn.Sequential(nn.Linear(hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1))

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, edge_attr: torch.Tensor) -> dict[str, torch.Tensor]:
        h = torch.relu(self.input_proj(x))
        for conv, norm in zip(self.convs, self.norms):
            h = norm(torch.relu(conv(h, edge_index, edge_attr)) + h)
        # log(1 - q) for each interval, accumulated: log P(survive to h).
        log_survive_step = F.logsigmoid(-self.head_hazard(h))
        log_survive = torch.cumsum(log_survive_step, dim=-1)
        # logit(p) = log(p) - log(1 - p), with p = 1 - exp(log_survive).
        log_p = torch.log(-torch.expm1(log_survive.clamp(max=-1e-6)))
        failure_logits = log_p - log_survive
        ttc = torch.sigmoid(self.head_ttc(h)).squeeze(-1) * TTC_MAX_SEC
        return {"failure_logits": failure_logits, "ttc": ttc, "embedding": h}


def build_model(architecture: dict) -> nn.Module:
    """Construct the network a manifest's `architecture` block describes."""
    cls = architecture.get("class", "HXCascade")
    if cls == "HXCascade":
        return HXCascade(node_feat_dim=int(architecture.get("node_feat_dim", 12)),
                         hidden=int(architecture.get("hidden", 64)),
                         num_layers=int(architecture.get("num_layers", 2)),
                         num_edge_types=int(architecture.get("num_edge_types", 6)))
    if cls == "HXCascadeV3":
        return HXCascadeV3(node_feat_dim=int(architecture["node_feat_dim"]),
                           edge_feat_dim=int(architecture["edge_feat_dim"]),
                           hidden=int(architecture.get("hidden", 64)),
                           num_layers=int(architecture.get("num_layers", 3)),
                           heads=int(architecture.get("heads", 4)),
                           dropout=float(architecture.get("dropout", 0.1)))
    raise ValueError(f"unknown model class {cls!r}")
