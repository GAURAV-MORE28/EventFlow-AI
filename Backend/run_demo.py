"""Demo-safe entry point:  python run_demo.py [--host 0.0.0.0] [--port 8000]

Same app and port as `run.py`, but ONE process and NO `--reload`:

* a file save no longer restarts the simulation mid-demo (with --reload every
  save restarts the worker at cycle 0);
* there is no reloader parent that can outlive a crashed worker and keep the
  port bound (observed in FINAL_AUDIT_REPORT.md: `[Errno 48] Address already
  in use` with no worker serving);
* Ctrl-C / SIGTERM runs the normal lifespan shutdown (engine stopped cleanly),
  and the port is free again as soon as the process exits.

`run.py` is unchanged and remains the developer entry point.
"""
from __future__ import annotations

import argparse
import socket
import sys

import uvicorn


def _port_in_use(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((probe_host, port)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="EventFlow AI backend — demo mode (no reload)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if _port_in_use(args.host, args.port):
        print(
            f"port {args.port} is already in use — another backend (or a stale `run.py` reloader) "
            f"is still running. Find it with: lsof -iTCP:{args.port} -sTCP:LISTEN",
            file=sys.stderr,
        )
        return 1

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
