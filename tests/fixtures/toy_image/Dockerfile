# Tiny image for sandbox and end-to-end tests: Python plus git (python:3.12-slim has no git).
# The test suite of the toy repo uses the standard library only, because the sandbox has no network.
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /testbed
