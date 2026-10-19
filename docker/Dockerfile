# The harness-lab service (CLAUDE.md section 3.1): python:3.12-slim, uv copied from the official
# image, dependencies from the lock file without the dev group. No pip.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.13 /uv /uvx /bin/

# git: only for tasks submitted as an https git URL (`git clone` on the service side)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# dependencies first, so editing the code does not re-install them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# configs by name; the toy repository, its config and its recording for the replay demo
COPY configs ./configs
COPY tests/fixtures/toy_config.yaml ./tests/fixtures/toy_config.yaml
COPY tests/fixtures/toy_repo ./tests/fixtures/toy_repo
COPY tests/fixtures/replays ./tests/fixtures/replays

EXPOSE 8000
CMD ["uv", "run", "--frozen", "--no-dev", "uvicorn", "--factory", "harness.service.api:create_app", "--host", "0.0.0.0", "--port", "8000"]
