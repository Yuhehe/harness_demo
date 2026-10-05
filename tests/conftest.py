import os

# Price data must come from the installed LiteLLM package (pinned by uv.lock), not from a
# GitHub fetch at import time: deterministic and works offline in CI.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
