import os

import pytest

# Price data must come from the installed LiteLLM package (pinned by uv.lock), not from a
# GitHub fetch at import time: deterministic and works offline in CI.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

TOY_IMAGE = "harness-toy:latest"


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
