FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV WORKSPACE_BENCH_ROOT=/workspace/Workspace-Bench
ENV WORKSPACE_BENCH_EVAL_ROOT=/workspace/Workspace-Bench/evaluation
ENV RIP_BENCH_ROOT=/workspace/Workspace-Bench
ENV RIP_BENCH_EVAL_ROOT=/workspace/Workspace-Bench/evaluation
ENV CODEX_SANDBOX_MODE=danger-full-access

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    unzip \
    wget \
    build-essential \
    pkg-config \
    libssl-dev \
    libgtk-3-dev \
    libwebkit2gtk-4.1-dev \
    libayatana-appindicator3-dev \
    librsvg2-dev \
    patchelf \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable

RUN python3 -m pip install --no-cache-dir --break-system-packages \
    tqdm \
    pyyaml \
    uv

RUN npm install -g openclaw @openai/codex@0.130.0

ENV PATH="/root/.cargo/bin:${PATH}"
ENV CARGO_HOME=/root/.cargo
ENV RUSTUP_HOME=/root/.rustup
ENV YIYI_OPENDATABOX_PROJECT_ROOT=/workspace/OpenDataBox
ENV YIYI_CARGO_ROOT=/workspace/OpenDataBox/YiYi/app/src-tauri
ENV YIYI_DOCKER_TARGET_DIR=/workspace/.cargo-target/yiyi
ENV YIYI_EVAL_BIN=/workspace/.cargo-target/yiyi/debug/eval
ENV YIYI_HOME=/root/.yiyi

WORKDIR /workspace/Workspace-Bench/evaluation

CMD ["/bin/bash"]
