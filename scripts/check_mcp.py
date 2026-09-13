"""Compatibility entry point; installed users can run notification-mcp test."""

import argparse
import asyncio

from notification_mcp.diagnostics import check

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--timeout", type=float, default=30)
    arguments = parser.parse_args()
    asyncio.run(check(arguments.url, arguments.timeout))
