"""Serve the dashboard on the Case 01 seed run with no database, for design work.

Uses the same stand-in data as tests/test_dashboard.py (run 1, nothing decided yet). Forms post but
change nothing. From the project folder:
    .venv\\Scripts\\python tools\\preview_seed.py          # http://127.0.0.1:8765
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import uvicorn  # noqa: E402
from _pytest.monkeypatch import MonkeyPatch  # noqa: E402

import test_dashboard as seed  # noqa: E402


def main(port: int = 8765) -> None:
    fixture = getattr(seed.app_client, "_get_wrapped_function", None)
    make_client = fixture() if fixture else seed.app_client.__wrapped__
    make_client(MonkeyPatch())                     # applies the stand-in queries; the client itself is unused
    uvicorn.run(seed.main.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main(int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765)))
