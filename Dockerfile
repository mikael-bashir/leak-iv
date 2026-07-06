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

# Install FastMCP
RUN uv pip install fastmcp asyncio nest_asyncio

# Clone PyPantograph (WITH submodules) to a separate folder and install it into our venv
WORKDIR ${HOME}/PyPantograph
RUN git clone --recurse-submodules https://github.com/stanford-centaur/PyPantograph.git .

RUN cp ${HOME}/app/lean-toolchain ./src/lean-toolchain
RUN python3 build-pantograph.py
RUN uv pip install .

# 8. Setup Lean Mathlib Cache
WORKDIR ${HOME}/app

RUN lake update

# CRITICAL: Fetch pre-compiled Mathlib binaries during the image build.
# If you skip this, your first FastMCP request will hang for 3 hours compiling math.
RUN lake exe cache get
RUN lake build

# 9. Environment Variables
EXPOSE 7860

# 10. Boot the server using the virtual environment
# Because we added the venv to the PATH in step 7, 'python3' will automatically use it.
CMD ["python3", "server.py"]