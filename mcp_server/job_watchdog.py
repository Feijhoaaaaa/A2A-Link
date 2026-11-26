"""Keep an active named Windows Job Object discoverable across MCP restarts."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from mcp_server.supervisor import IS_WINDOWS, WindowsJob


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3 or not IS_WINDOWS:
        return 2
    name, ready_path, release_path = argv
    job = None
    try:
        job = WindowsJob.open(name)
        path = Path(ready_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ready\n", encoding="ascii")
        release = Path(release_path)
        while not release.exists():
            time.sleep(0.05)
        return 0
    except Exception:
        return 1
    finally:
        if job is not None:
            job.close()


if __name__ == "__main__":
    raise SystemExit(main())
