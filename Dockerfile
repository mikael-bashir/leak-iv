# 1. Base Image
FROM ubuntu:22.04

# 2. CREATE THE GUEST USER (Required for Hugging Face Spaces permissions)
RUN useradd -m -u 1000 user

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

# 3. Install System Dependencies
# Added python3-venv and cmake (needed to compile PyPantograph's C++ bindings)
RUN apt-get update && apt-get install -y \
    curl git build-essential python3 python3-pip python3-venv cmake tzdata && \
    rm -rf /var/lib/apt/lists/*

# 4. Switch to the unprivileged user
USER user
ENV HOME=/home/user
# Put elan (Lean) and uv in the PATH
ENV PATH="${HOME}/.local/bin:${HOME}/.elan/bin:${PATH}"

# 5. Install Lean (elan) & Python package manager (uv)
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# 6. Setup Workspace & Copy Files
# NOTE: Your local directory being copied MUST contain your 'server.py', 'lakefile.lean', and 'lean-toolchain'
WORKDIR ${HOME}/app
COPY --chown=user . ${HOME}/app

# So that file watcher doesn't crash, and to avoid permission errors later
RUN touch ${HOME}/app/virtual_sandbox.lean

# 7. Setup Python Virtual Environment & Install Dependencies
# Create a venv directly in the app folder and add it to the PATH
RUN uv python install 3.11
RUN uv venv --python 3.11 ${HOME}/app/.venv
ENV PATH="${HOME}/app/.venv/bin:${PATH}"

# Install FastMCP. "mcp<2" pinned: mcp 2.x renamed FastMCP to MCPServer and
# changed its API, breaking server.py's `from mcp.server.fastmcp import
# FastMCP` import.
RUN uv pip install fastmcp "mcp<2" asyncio nest_asyncio

# NOTE: this Dockerfile used to also clone and build PyPantograph here, but
# server.py drives `lake serve`'s own LSP protocol directly (see its own
# "no interactive Pantograph" comment) and never imports the pantograph
# package — that block only existed as unused copy-paste from Leak-II's
# Dockerfile, and every day it stayed was a toolchain-upgrade risk this
# service never actually needed to carry. Removed.

# 8. The environment: the Tengoku tree — one self-contained library seeded from
# Mathlib, no Lake dependencies. Its published build cache replaces
# `lake exe cache get`; `tengoku_sync` (an MCP tool) repeats these three
# steps at runtime whenever the tree has grown.
USER root
RUN apt-get update && apt-get install -y zstd && rm -rf /var/lib/apt/lists/*
USER user
WORKDIR ${HOME}/app
# Full history without blobs: the cache script picks the newest published
# cache that is an ancestor of HEAD, which a depth-1 clone cannot answer.
# Changing this build arg (the installer passes the current time) invalidates
# Docker's layer cache from here down, so a re-run re-clones and re-pins to the
# newest cache instead of reusing a stale clone layer.
ARG TENGOKU_REFRESH=0
RUN echo "refresh ${TENGOKU_REFRESH}" >/dev/null && git clone --filter=blob:none https://github.com/competemath/tengoku.git tengoku
ENV LEAN_PROJECT_PATH=${HOME}/app/tengoku
# gh needs a token to read release assets at build time: pass GH_TOKEN as a build secret.
# Pin the checkout to the commit of the newest published cache, then fetch
# that cache (anonymously — the tree is public; a GH_TOKEN build secret only
# raises the API rate limit). With sources and cache at the same commit the
# build below is a pure replay: nothing is compiled. The cache is refreshed
# regularly, so this lags the tree by little; `tengoku_sync` moves forward.
# Pin the tree to its newest published cache and replay it: nothing compiles.
# The same script runs at container start (so a nightly cache published while
# a Space slept is picked up then) and behind tengoku_sync / POST /refresh.
RUN --mount=type=secret,id=GH_TOKEN,env=GH_TOKEN,required=false cd tengoku && scripts/pin.sh
ENV TENGOKU_IMPORTS="import Tengoku.All"
RUN touch ${HOME}/app/tengoku/virtual_sandbox.lean

# 9. Environment Variables
EXPOSE 7860

# 10. Boot the server using the virtual environment
# Because we added the venv to the PATH in step 7, 'python3' will automatically use it.
CMD ["python3", "server.py"]