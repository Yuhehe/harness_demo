"""Token counting for the context budget.

`context.budget_tokens` limits the prompt we send, so the manager needs the size of the NEXT
prompt before the call. The provider only reports it afterwards, so two numbers are combined:

* a local count (LiteLLM's counter for the model; DeepSeek has no public tokenizer there, so
  it is an approximation), and
* the provider's own `prompt_tokens` for the previous request, which anchors the estimate:
  next prompt = previous provider count + local count of the messages added since.

The anchor makes the estimate exact for everything already sent; only the new messages are
approximated. After a compaction there is no anchor, and the local count is scaled by the last
provider/local ratio instead.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import litellm

from harness.llm import cost
from harness.llm.types import Message, strip_reasoning

_RATIO_MIN, _RATIO_MAX = 0.5, 2.0


def count_messages(model: str, messages: list[Message]) -> int:
    """Local token count of messages. Never raises: an estimate must not break a run."""
    messages = strip_reasoning(messages)
    try:
        return int(
            litellm.token_counter(model=cost.normalize_model(model), messages=messages)
        )
    except Exception:  # noqa: BLE001
        return len(json.dumps(messages, default=str)) // 4


class TokenEstimator:
    def __init__(self, model: str, tools: list[dict[str, Any]] | None) -> None:
        self.model = model
        self.tools = tools
        self.ratio = (
            1.0  # provider tokens / local tokens, from the latest anchored request
        )
        self._tools_tokens: int | None = None

    def _local(self, messages: list[Message]) -> int:
        return count_messages(self.model, messages) if messages else 0

    async def tools_tokens(self) -> int:
        """Tool definitions are part of every prompt. LiteLLM's counter ignores `tools` for
        DeepSeek models (measured: the same count with and without), so the serialized
        definitions are counted as text."""
        if self._tools_tokens is None:
            text = json.dumps(self.tools or [], sort_keys=True)
            self._tools_tokens = (
                await asyncio.to_thread(
                    count_messages, self.model, [{"role": "user", "content": text}]
                )
                if self.tools
                else 0
            )
        return self._tools_tokens

    async def local(self, messages: list[Message]) -> int:
        """Local count of `messages` as part of a prompt (tool definitions included)."""
        return (
            await asyncio.to_thread(self._local, messages) + await self.tools_tokens()
        )

    async def calibrate(self, sent: list[Message], provider_prompt_tokens: int) -> None:
        """Learn the provider/local ratio from a request whose real size is now known."""
        local = await self.local(sent)
        if local > 0 and provider_prompt_tokens > 0:
            self.ratio = min(
                max(provider_prompt_tokens / local, _RATIO_MIN), _RATIO_MAX
            )

    async def estimate(
        self,
        messages: list[Message],
        *,
        anchor_len: int | None = None,
        anchor_tokens: int = 0,
    ) -> int:
        """Size of the prompt made of `messages`. With an anchor, the first `anchor_len`
        messages are known to cost `anchor_tokens`; only the rest is counted locally."""
        if anchor_len is not None and 0 < anchor_len <= len(messages):
            tail = await asyncio.to_thread(self._local, messages[anchor_len:])
            return anchor_tokens + round(tail * self.ratio)
        return round(await self.local(messages) * self.ratio)

    async def local_each(self, messages: list[Message]) -> list[int]:
        """Local count of each message on its own (to see what clearing one would save)."""
        return await asyncio.to_thread(lambda: [self._local([m]) for m in messages])

    async def text_tokens(self, text: str) -> int:
        """Size of a piece of text as part of a prompt (local count times the ratio)."""
        local = await asyncio.to_thread(
            count_messages, self.model, [{"role": "user", "content": text}]
        )
        return round(local * self.ratio)
