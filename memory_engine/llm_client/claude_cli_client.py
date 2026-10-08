"""
MemoryEngine's LLM calls answered by Claude through an injected transport.

The application decides how Claude is reached (phro-secretary runs the `claude` CLI as an isolated, metered
subprocess per call: no tools, no settings, no memory). This client only turns MemoryEngine's messages into a
system/user prompt pair, picks the model by MemoryEngine's model size, and reads the JSON answer back.
"""

import asyncio
import json
import logging
import re
import typing
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from ..prompts.models import Message
from .client import LLMClient
from .config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize

logger = logging.getLogger(__name__)

# transport(system, prompt, model) -> raw reply text. Blocking; it runs in a worker thread.
Transport = Callable[[str, str, str], str]


def last_json_object(text: str) -> dict[str, typing.Any] | None:
    """The last top-level JSON object in a reply that may reason first (and cite things like "[27]")."""
    decoder = json.JSONDecoder()
    found, end = None, 0
    for match in re.finditer(r'\{', text):
        if match.start() < end:
            continue
        try:
            value, end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict):
            found = value
    return found


class ClaudeCLIClient(LLMClient):
    def __init__(self, transport: Transport, config: LLMConfig | None = None, cache: bool = False):
        # Model names are whatever the transport understands ("sonnet", "haiku" for the claude CLI).
        super().__init__(config or LLMConfig(api_key='unused', model='sonnet', small_model='haiku'), cache)
        self.transport = transport

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, typing.Any]:
        system = '\n\n'.join(m.content for m in messages if m.role == 'system')
        prompt = '\n\n'.join(m.content for m in messages if m.role != 'system')
        system += '\n\nAnswer with the JSON object only.'
        model = (self.small_model if model_size == ModelSize.small else self.model) or 'sonnet'
        text = await asyncio.to_thread(self.transport, system, prompt, model)
        data = last_json_object(text)
        if data is None:
            logger.error(self._get_failed_generation_log(messages, text))
            raise ValueError('Claude reply contained no JSON object')
        if response_model is None:
            return data
        try:
            return response_model.model_validate(data).model_dump()
        except ValidationError as exc:
            logger.error(self._get_failed_generation_log(messages, text))
            raise ValueError(f'Claude reply does not match {response_model.__name__}: {exc}') from exc
