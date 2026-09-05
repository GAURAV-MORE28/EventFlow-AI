"""Reference ML modules — a stand-in, not the deliverable.

These implement the interfaces in `03_ML_CONTRACT.md` exactly, so the backend and
frontend are fully runnable and demoable before the ML workstream ships. They are
deliberately the *deterministic* branch of every ladder in the contract:

    forecaster   -> persistence / local_model (no TSFM weights here)
    cascade      -> deterministic propagator  (no GNN here)
    twin         -> real EnKF, aggregate state
    equilibrium  -> real fixed-point solver, certify() only
    optimiser    -> rule-based templates
    risk/anomaly -> arithmetic, exactly as the contract requires

`app/ml_registry.py` prefers the real `ml/` package whenever it is importable, so
dropping `ml/forecaster.py` into the repo swaps the implementation with no backend
change. Nothing in here imports from `app/` beyond this package — the dependency
arrow in `00_SHARED_CONTRACT.md` points one way (ml <- backend <- frontend).
"""

from .anomaly import AnomalyDetector  # noqa: F401
from .cascade import CascadePredictor  # noqa: F401
from .equilibrium import EquilibriumSolver, derive_verdict, row_verdict  # noqa: F401
from .forecaster import Forecaster  # noqa: F401
from .generator import SyntheticGenerator  # noqa: F401
from .optimiser import InterventionOptimiser  # noqa: F401
from .risk import RiskScorer  # noqa: F401
from .twin import AssimilatedTwin  # noqa: F401

__all__ = [
    "AnomalyDetector", "CascadePredictor", "EquilibriumSolver", "Forecaster",
    "InterventionOptimiser", "RiskScorer", "SyntheticGenerator", "AssimilatedTwin",
    "derive_verdict", "row_verdict",
]
