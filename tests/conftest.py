import os

import pytest

# Price data must come from the installed LiteLLM package (pinned by uv.lock), not from a
# GitHub fetch at import time: deterministic and works offline in CI.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

TOY_IMAGE = "harness-toy:latest"

# The entry points load .env (python-dotenv never overrides a variable that is already set).
# Tests must not depend on a developer's .env, which a clean clone and CI do not have, and must
# never reach a real API with a real key: fix the models and point the keys and base URLs at a
# closed local port. Tests that start a fake server set their own base URL on top.
TEST_ENV = {
    "MODEL_MAIN": "deepseek/deepseek-v4-pro",
    "MODEL_CHEAP": "deepseek/deepseek-flash",
    "DEEPSEEK_API_KEY": "test-key-not-real",
    "DEEPSEEK_API_BASE": "http://127.0.0.1:9",
    "OPENAI_API_KEY": "test-key-not-real",
    "OPENAI_API_BASE": "http://127.0.0.1:9",
    "HARNESS_OTEL": "0",  # never export test runs (tracing/otel.py), whatever .env says
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    for key in [k for k in os.environ if k.startswith("OTEL_EXPORTER_OTLP")]:
        monkeypatch.delenv(key)


@pytest.fixture(scope="session")
def docker_client():
    """A Docker client with the toy image present, or skip the test."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(TOY_IMAGE)
    except Exception as e:  # noqa: BLE001 - any failure means "cannot run Docker tests here"
        pytest.skip(
            f"Docker or {TOY_IMAGE} not available ({type(e).__name__}); "
            "build it with: docker build -t harness-toy tests/fixtures/toy_image"
        )
    return client
