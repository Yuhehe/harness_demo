"""harness-lab: a from-scratch coding-agent harness."""

import os

# Prices must come from the installed LiteLLM version (pinned by uv.lock), never from a
# GitHub fetch at import time: deterministic, and it works offline. This has to be set
# before litellm is imported anywhere.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
