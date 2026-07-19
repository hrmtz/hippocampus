"""SSE transport runner for hippocampus MCP server.

Wraps the FastMCP SSE app with Bearer token auth middleware and runs via uvicorn.

Usage (via run_server_sse.sh):
  MCP_SSE_TOKEN=<token> FASTMCP_PORT=8091 python3 -m hippocampus.sse
"""
import hmac
import os
import sys

import uvicorn
from starlette.responses import Response

from .server import _gate_tools, mcp

MCP_SSE_TOKEN = os.environ.get("MCP_SSE_TOKEN", "")
HOST = os.environ.get("FASTMCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("FASTMCP_PORT", "8091"))


class _BearerAuth:
    """ASGI middleware: require Authorization: Bearer <MCP_SSE_TOKEN>."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode()
            expected = f"Bearer {MCP_SSE_TOKEN}"
            if not MCP_SSE_TOKEN or not hmac.compare_digest(auth, expected):
                resp = Response("Unauthorized", status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main() -> None:
    """Entry point for the SSE transport (same gated tool set as stdio)."""
    if not MCP_SSE_TOKEN:
        print("ERROR: MCP_SSE_TOKEN not set", file=sys.stderr, flush=True)
        sys.exit(1)

    _gate_tools()
    app = _BearerAuth(mcp.sse_app())

    print(f"[hippocampus-mcp-sse] listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
