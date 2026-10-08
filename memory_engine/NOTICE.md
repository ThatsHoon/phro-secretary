# memory_engine

phro-secretary's graph memory engine: entity/edge extraction, deduplication, temporal contradiction handling and
hybrid search over FalkorDB, with every LLM call answered by Claude through the application's metered CLI
transport and embeddings from the local Ollama endpoint.

## Attribution

Derived from Graphiti `graphiti-core` 0.30.2 by Zep Software, Inc. (https://github.com/getzep/graphiti),
licensed under the Apache License 2.0 (`LICENSE`). Files carrying the Zep Software copyright header originate
there; the license requires keeping those headers and this notice.

Modifications by phro-secretary:

- Package renamed `memory_engine`; the engine class is `MemoryEngine` (`engine.py`), its client bundle
  `EngineClients` (`engine_types.py`), the base error `MemoryEngineError`.
- `llm_client/claude_cli_client.py` (new): `ClaudeCLIClient` answers every LLM call with Claude through an
  injected transport, so each call is logged in `llm_calls` like the rest of the app's Claude use.
- `cross_encoder/claude_unavailable_client.py` (new): `NoCrossEncoder` raises if a cross-encoder search recipe
  is used; phro-secretary searches with RRF only.
- `MemoryEngine` takes its driver, LLM client, embedder and cross-encoder explicitly; there are no default
  providers.
- Removed: usage telemetry, the Neo4j/Kuzu/Neptune drivers, the OpenAI/Anthropic/Gemini/Groq/Azure/GLiNER LLM
  clients, the Gemini/Voyage/Azure embedders, the OpenAI/Gemini/BGE rerankers, migrations, content chunking.

After changing anything here, run the live tests (`PHRO_LIVE_TEST=1 pytest tests/test_graph_live.py`).
