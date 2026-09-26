# Change lock — final hardening pass

Scope: correctness, consistency, determinism and stability of the existing
product. Branch: `Paresh` only; no force push; push only when the owner says so.

## ALLOWED

- backend bug fixes
- simulation correctness fixes
- flow propagation fixes
- state consistency fixes
- event demand propagation
- crowd/venue corrections
- cascade corrections
- intervention corrections
- What-If corrections
- attendee travel-time corrections
- WebSocket/state synchronization fixes
- deterministic calculations
- backend tests
- API validation/error handling
- frontend changes required to correctly display authoritative backend state
- removal of the duplicated landing-page panels (Commander, What-If, Pressure
  Timeline, Intervention Queue, Digital Twin) from the Command Centre
- event management (create / edit / cancel / delete, exact datetimes, windows)
  and immediate reconciliation of operator changes into the live state

## NOT ALLOWED

- retraining ML
- redesigning R-GCN
- replacing ML architecture
- inventing additional AI models
- adding unrelated features
- unnecessary UI redesign
- replacing the existing simulation architecture
- rewriting working code merely for style
- changing branches
- force pushing
- deleting working functionality
- modifying unrelated files without reason

## Contract rules still in force

- Wire contract changes are additive only (new optional fields / new WS
  message types); no renames, removals or type changes.
- Every derived value has one owner (see `implementationstate.md §2`).
- `ML/` is not modified in this pass.
