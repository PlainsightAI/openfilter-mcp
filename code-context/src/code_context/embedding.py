"""
This module provides functionality for generating embeddings using a pre-trained model.
"""

import numpy as np
from llama_cpp import Llama

# Global variables for lazy-loaded models
_model = None

# Instruction prefixes for Jina code embeddings model
INSTRUCTION_CONFIG = {
    "nl2code": {
        "query": "Find the most relevant code snippet given the following query:\n",
        "passage": "Candidate code snippet:\n",
    },
    "qa": {
        "query": "Find the most relevant answer given the following question:\n",
        "passage": "Candidate answer:\n",
    },
    "code2code": {
        "query": "Find an equivalent code snippet given the following code snippet:\n",
        "passage": "Candidate code snippet:\n",
    },
    "code2nl": {
        "query": "Find the most relevant comment given the following code snippet:\n",
        "passage": "Candidate comment:\n",
    },
    "code2completion": {
        "query": "Find the most relevant completion given the following start of code snippet:\n",
        "passage": "Candidate completion:\n",
    },
}


def get_model():
    """Lazy-load the Llama model only when needed."""
    global _model
    if _model is None:
        _model = Llama.from_pretrained(
            "jinaai/jina-code-embeddings-1.5b-GGUF",
            filename="jina-code-embeddings-1.5b-Q8_0.gguf",
            embedding=True,
            n_gpu_layers=-1,
            n_ctx=32768,
            n_batch=32768,
        )
    return _model


def get_embedding(text):
    """Generate a single-vector embedding using jina-code-embeddings-1.5b.

    The model uses embeddings-last format - we take the last token's embedding.
    """
    model = get_model()

    # Get the embedding - llama.cpp returns token-level embeddings
    embedding = model.create_embedding(text)["data"][0]["embedding"]
    embedding = np.array(embedding, dtype=np.float32)

    # If we got multiple token embeddings, take the last one (embeddings-last format)
    if embedding.ndim == 2:
        embedding = embedding[-1]

    # Normalize the embedding
    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm

    return embedding

def get_embeddings(texts, progress_callback=None):
    embeddings = []
    for i, text in enumerate(texts):
        embeddings.append(get_embedding(text))
        if progress_callback:
            progress_callback(i + 1, len(texts))
    return embeddings
