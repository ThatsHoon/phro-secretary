"""Local text embeddings: nomic-embed-text v1.5 run in-process with onnxruntime (no separate model server).

The weights are not in Git (models/NOTICE.md). Fetch them, pinned and hash-checked, with
    .venv/Scripts/python.exe -m memory.embedder
The installer ships them; PHRO_MODEL_DIR points elsewhere.
"""
from functools import cache
from pathlib import Path
from urllib.request import urlopen
import hashlib
import os
import sys
import threading

import numpy as np

MODEL = 'nomic-embed-text-v1.5'
DIMENSION = 768
SOURCE = 'https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/resolve/e9b6763023c676ca8431644204f50c2b100d9aab/'
# fp16 matches the fp32 reference (cosine 1.0); the int8 export drifted ~4% (docs/troubleshooting.md).
FILES = {'model_fp16.onnx': ('onnx/model_fp16.onnx', 'cf5b5a86edb00f895561803cfc04729090a958340b8ca2ad76c143f565f6bb04'),
         'tokenizer.json': ('tokenizer.json', 'd241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66')}
MAX_TOKENS = 2048


def model_dir():
    return Path(os.environ.get('PHRO_MODEL_DIR') or Path(__file__).resolve().parent.parent / 'models' / MODEL)


class Embedder:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.session = self.tokenizer = None

    def _load(self):
        with self.lock:
            if self.session is None:
                missing = [name for name in FILES if not (self.folder / name).is_file()]
                if missing:
                    raise FileNotFoundError(f'embedding model missing in {self.folder}: {", ".join(missing)}')
                import onnxruntime
                from tokenizers import Tokenizer
                tokenizer = Tokenizer.from_file(str(self.folder / 'tokenizer.json'))
                tokenizer.enable_truncation(MAX_TOKENS)
                self.session = onnxruntime.InferenceSession(str(self.folder / 'model_fp16.onnx'),
                                                            providers=['CPUExecutionProvider'])
                self.tokenizer = tokenizer

    def embed(self, text, query=False):
        """(unit vector, token count) for one text: mean pooling over tokens, as the model was trained.

        The model was trained with task prefixes: questions and the stored texts they search take different ones."""
        if self.session is None:
            self._load()
        encoding = self.tokenizer.encode(('search_query: ' if query else 'search_document: ') + text)
        ids = np.array([encoding.ids], dtype=np.int64)
        mask = np.array([encoding.attention_mask], dtype=np.int64)
        hidden = self.session.run(None, {'input_ids': ids, 'attention_mask': mask,
                                         'token_type_ids': np.zeros_like(ids)})[0][0]
        vector = hidden.mean(axis=0).astype(np.float32)
        return vector / np.linalg.norm(vector), len(encoding.ids)


@cache
def shared(folder=None):
    """One loaded model per process (about 0.5 GB in memory once used)."""
    return Embedder(folder or model_dir())


def download(folder):
    """Fetch the pinned files into folder; files already there with the right hash are kept."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, (path, digest) in FILES.items():
        target = folder / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        partial = target.with_suffix('.part')
        sha = hashlib.sha256()
        with urlopen(SOURCE + path, timeout=60) as response, open(partial, 'wb') as out:
            while chunk := response.read(1 << 20):
                sha.update(chunk)
                out.write(chunk)
        if sha.hexdigest() != digest:
            partial.unlink()
            raise ValueError(f'{name} changed upstream; review before updating the pinned hash')
        partial.replace(target)
        print('fetched', target)


if __name__ == '__main__':
    download(sys.argv[1] if len(sys.argv) > 1 else model_dir())
