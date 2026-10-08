# Embedding model

`nomic-embed-text-v1.5/` holds the text embedding model phro-secretary runs in-process (`memory/embedder.py`).
The weights are not in Git; `.venv/Scripts/python.exe -m memory.embedder` fetches them, and the installer ships them.

- Model: nomic-embed-text-v1.5 by Nomic AI, https://huggingface.co/nomic-ai/nomic-embed-text-v1.5
- Files: `onnx/model_fp16.onnx`, `tokenizer.json` at revision `e9b6763023c676ca8431644204f50c2b100d9aab`
  (SHA-256 pinned in `memory/embedder.py`), unmodified.
- License: Apache License 2.0 ([LICENSE](LICENSE)).
