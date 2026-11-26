import logging
import os
import sys

from mcp_server.server import build_server


def main() -> None:
    # stdout carries the MCP stdio protocol; logs must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    build_server(os.environ.get("A2A_LINK_POLICY")).run("stdio")


if __name__ == "__main__":
    main()
