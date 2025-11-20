"""
Code Context: A library for chunking, embedding, and indexing code.
"""

from .chunking import (
    chunk_document,
    chunk_document_ast,
    convert_ipynb_to_python,
)
from .embedding import (
    get_embedding,
    get_embeddings,
)
from .indexing import (
    _do_index as index_repo,
)
from .main import (
    main,
    index,
    get_index_status,
    list_indexing_jobs,
    list_indexes,
    search,
    search_code,
    get_chunk,
    _search,
    _search_code,
    _get_chunk,
)

__all__ = [
    "chunk_document",
    "chunk_document_ast",
    "convert_ipynb_to_python",
    "get_embedding",
    "get_embeddings",
    "index_repo",
    "main",
    "index",
    "get_index_status",
    "list_indexing_jobs",
    "list_indexes",
    "search",
    "search_code",
    "get_chunk",
    "_search",
    "_search_code",
    "_get_chunk",
]