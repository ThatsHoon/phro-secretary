"""
No cross-encoder. MemoryEngine would otherwise need a reranker on an OpenAI-compatible chat model
(the local model phro-secretary does not run). Searches that need one fail loudly instead of reranking silently.
"""

from .client import CrossEncoderClient


class NoCrossEncoder(CrossEncoderClient):
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        raise NotImplementedError('cross-encoder reranking is not configured (use an RRF search recipe)')
