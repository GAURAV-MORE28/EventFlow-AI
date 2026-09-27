"""Online evaluation of failure predictors, one definition for all of them.

The event is physical and the same one the cascade model is trained on
(`scripts/train_cascade.py` labels): an entity of a scored type crossing its
critical utilisation line. Every cycle, each predictor flags entities; each
flag of an entity not over its line now is a *claim*: "this entity will cross
within `horizon_sec`". A claim is resolved when

  * the entity crosses within `horizon_sec` of it  -> confirmed
  * `horizon_sec` passes without a crossing        -> false alarm

    precision = confirmed claims / resolved claims
    recall    = crossings with a claim in the `horizon_sec` before them / all crossings
    lead time = crossing - earliest claim in that window (so at most `horizon_sec`)

This is the per-snapshot definition of the offline evaluation
(`ML/evaluation/cascade_eval.py`). Claims are judged one by one on purpose: an
alert first raised a little more than one horizon before its crossing is one
wrong claim, not a wrong alert followed by a short-lead right one, and a
predictor cannot buy precision by keeping every entity flagged.

Crossings are counted per predictor only while that predictor is active, so a
model that is switched off is not charged with misses. In simulation only —
never presented as field validity (03 §8.4).
"""
from __future__ import annotations

from typing import Any

SCORED_TYPES = ("gate", "road", "transport_node", "emergency_facility")


class OnlineEvaluator:
    def __init__(self, horizon_sec: int = 3600) -> None:
        self.horizon_sec = int(horizon_sec)
        self.reset()

    def reset(self) -> None:
        # predictor -> entity -> cycles of its unresolved claims (ascending)
        self._pending: dict[str, dict[str, list[int]]] = {}
        self._stats: dict[str, dict[str, Any]] = {}
        self._was_over: dict[str, bool] = {}

    def _predictor(self, name: str) -> dict[str, Any]:
        self._pending.setdefault(name, {})
        return self._stats.setdefault(name, {"confirmed": 0, "false_alarms": 0, "caught": 0, "missed": 0,
                                             "lead_times_sec": []})

    def update(self, cycle: int, cycle_sec: int, over_line: dict[str, bool],
               alerts: dict[str, set[str]]) -> None:
        """`over_line`: scored entity -> at/above its critical line now.
        `alerts`: active predictor -> entities it currently flags."""
        window = max(1, self.horizon_sec // max(cycle_sec, 1))
        for name in alerts:
            self._predictor(name)
        # 1. crossings confirm the claims made on the entity within the window
        for eid, over in over_line.items():
            if over and not self._was_over.get(eid, False):
                for name in alerts:
                    stats = self._stats[name]
                    claims = [c for c in self._pending[name].pop(eid, []) if cycle - c <= window]
                    if claims:
                        stats["confirmed"] += len(claims)
                        stats["caught"] += 1
                        stats["lead_times_sec"].append((cycle - claims[0]) * cycle_sec)
                        del stats["lead_times_sec"][:-200]
                    else:
                        stats["missed"] += 1
        # 2. claims whose window passed without a crossing
        for name, pending in self._pending.items():
            for eid, claims in list(pending.items()):
                expired = sum(1 for c in claims if cycle - c >= window)
                if expired:
                    self._stats[name]["false_alarms"] += expired
                    del claims[:expired]
                if not claims:
                    del pending[eid]
        # 3. this cycle's claims (an entity already over its line is not a prediction)
        for name, flagged in alerts.items():
            pending = self._pending[name]
            for eid in flagged:
                if eid in over_line and not over_line[eid]:
                    pending.setdefault(eid, []).append(cycle)
        self._was_over = dict(over_line)

    def summary(self, name: str) -> dict[str, Any]:
        s = self._stats.get(name) or {"confirmed": 0, "false_alarms": 0, "caught": 0, "missed": 0,
                                      "lead_times_sec": []}
        closed = s["confirmed"] + s["false_alarms"]
        events = s["caught"] + s["missed"]
        leads = s["lead_times_sec"][-50:]
        return {
            "precision": round(s["confirmed"] / closed, 3) if closed else 0.0,
            "precision_n": closed,
            "recall": round(s["caught"] / events, 3) if events else 0.0,
            "recall_n": events,
            "lead_time_sec": round(sum(leads) / len(leads), 0) if leads else 0.0,
            "lead_time_n": len(leads),
            "open_alerts": len(self._pending.get(name, {})),
        }
