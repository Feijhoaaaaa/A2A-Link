import logging
import os
import sys

from mcp_server.server import ISSUE_RUNNER, build_server


def main() -> None:
    # stdout carries the MCP stdio protocol; logs must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = build_server(os.environ.get("A2A_LINK_POLICY"))
    ISSUE_RUNNER.start_dispatcher()
    try:
        server.run("stdio")
    finally:
        ISSUE_RUNNER.stop_dispatcher(timeout=35)


if __name__ == "__main__":
    main()
