"""Online evaluation of failure predictors, one definition for all of them.

The event is physical and the same one the cascade model is trained on
(`scripts/train_cascade.py` labels): an entity of a scored type crossing its
critical utilisation line. Each predictor raises *alerts*: "this entity, not
over its line now, will cross it". An alert stays open until:

  * the entity crosses within `horizon_sec`      -> confirmed (lead time recorded)
  * `horizon_sec` passes without a crossing      -> false alarm

    precision = confirmed / (confirmed + false alarms)
    recall    = crossings with an open alert / all crossings

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
        self._open: dict[str, dict[str, int]] = {}
        self._stats: dict[str, dict[str, Any]] = {}
        self._was_over: dict[str, bool] = {}

    def _predictor(self, name: str) -> dict[str, Any]:
        self._open.setdefault(name, {})
        return self._stats.setdefault(name, {"confirmed": 0, "false_alarms": 0, "caught": 0, "missed": 0,
                                             "lead_times_sec": []})

    def update(self, cycle: int, cycle_sec: int, over_line: dict[str, bool],
               alerts: dict[str, set[str]]) -> None:
        """`over_line`: scored entity -> at/above its critical line now.
        `alerts`: active predictor -> entities it currently flags."""
        window = max(1, self.horizon_sec // max(cycle_sec, 1))
        for name in alerts:
            self._predictor(name)
        # 1. crossings resolve open alerts (or are misses)
        for eid, over in over_line.items():
            if over and not self._was_over.get(eid, False):
                for name in alerts:
                    stats = self._stats[name]
                    first = self._open[name].pop(eid, None)
                    if first is not None:
                        stats["confirmed"] += 1
                        stats["caught"] += 1
                        stats["lead_times_sec"].append((cycle - first) * cycle_sec)
                        del stats["lead_times_sec"][:-200]
                    else:
                        stats["missed"] += 1
        # 2. alerts whose horizon passed without a crossing
        for name, open_ in self._open.items():
            for eid, first in list(open_.items()):
                if cycle - first >= window:
                    self._stats[name]["false_alarms"] += 1
                    del open_[eid]
        # 3. new alerts (an entity already over its line is not a prediction)
        for name, flagged in alerts.items():
            open_ = self._open[name]
            for eid in flagged:
                if eid in over_line and not over_line[eid] and eid not in open_:
                    open_[eid] = cycle
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
            "open_alerts": len(self._open.get(name, {})),
        }
