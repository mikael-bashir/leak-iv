#!/bin/bash
set -e

# 1. Boot the MCP server in the background on the local port
/home/user/lean-lsp-mcp/.venv/bin/lean-lsp-mcp \
    --transport streamable-http \
    --port 8000 \
    --host 127.0.0.1 &

# 2. Boot Nginx in the foreground on the public port
nginx -c /home/user/app/nginx.conf