"""
This module provides a web service for code context, including indexing and search.
"""

import faiss
from fastmcp import FastMCP
import os
import queue
import threading
import traceback
from datetime import datetime
from typing import Any, Dict

from .embedding import INSTRUCTION_CONFIG, get_embedding
from .indexing import _do_index, CLONES_DIR, INDEXES_DIR

mcp = FastMCP(name="code-context: Context Server for Arbitrary Code")

# Global variables for async indexing status
_indexing_jobs: Dict[str, Dict[str, Any]] = {}
_indexing_lock = threading.Lock()

# Queue for sequential processing of indexing jobs
_indexing_queue = queue.Queue()
_indexing_worker_thread = None
_indexing_worker_lock = threading.Lock()


def _indexing_worker():
    """Worker thread that processes indexing jobs sequentially from the queue."""
    while True:
        job = _indexing_queue.get()
        if job is None:  # Sentinel value to stop the worker
            break

        job_id, repo_url, force, is_local = job
        try:
            _do_index(job_id, repo_url, force, is_local, _indexing_jobs, _indexing_lock)
        except Exception:
            # Errors are already handled in _do_index
            pass
        finally:
            _indexing_queue.task_done()


def _ensure_indexing_worker():
    """Ensures the indexing worker thread is running."""
    global _indexing_worker_thread
    with _indexing_worker_lock:
        if _indexing_worker_thread is None or not _indexing_worker_thread.is_alive():
            _indexing_worker_thread = threading.Thread(
                target=_indexing_worker, daemon=True
            )
            _indexing_worker_thread.start()


@mcp.tool()
def index(repo_url: str, force: bool = False) -> str:
    """Starts indexing a Git repository from a URL or local path asynchronously.

    Args:
        repo_url: Git repository URL (e.g., https://github.com/user/repo) or absolute local path
        force: If True, removes and re-indexes even if already indexed

    Returns a job ID to track progress.
    """
    # Ensure the worker thread is running
    _ensure_indexing_worker()

    # Detect if this is a local path (starts with / on Unix or C:\ on Windows)
    is_local = os.path.isabs(repo_url) or repo_url.startswith("/")

    if is_local:
        base_name = os.path.basename(repo_url.rstrip("/"))
    else:
        base_name = os.path.basename(repo_url).replace(".git", "")

    job_id = f"{base_name}-{datetime.now().timestamp()}"

    with _indexing_lock:
        _indexing_jobs[job_id] = {
            "job_id": job_id,
            "repo_url": repo_url,
            "status": "queued",
            "message": "Indexing job queued, waiting for worker",
            "started_at": datetime.now().isoformat(),
            "force": force,
        }

    # Add job to the queue for sequential processing
    _indexing_queue.put((job_id, repo_url, force, is_local))

    source_type = "local directory" if is_local else "repository"
    return f"Indexing job queued for {source_type} with ID: {job_id}. Jobs are processed sequentially. Use get_index_status('{job_id}') to check progress."


@mcp.tool()
def get_index_status(job_id: str) -> Dict[str, Any]:
    """Gets the status of an indexing job by its job ID."""
    try:
        with _indexing_lock:
            if job_id not in _indexing_jobs:
                return {
                    "error": f"Job ID '{job_id}' not found",
                    "available_jobs": list(_indexing_jobs.keys()),
                }
            return _indexing_jobs[job_id].copy()
    except Exception as e:
        return {
            "error": f"Error getting index status: {str(e)}",
            "traceback": traceback.format_exc(),
        }


@mcp.tool()
def list_indexing_jobs() -> Dict[str, Dict[str, Any]]:
    """Lists all indexing jobs and their current status."""
    try:
        with _indexing_lock:
            return {
                job_id: job_info.copy() for job_id, job_info in _indexing_jobs.items()
            }
    except Exception as e:
        return {
            "error": f"Error listing indexing jobs: {str(e)}",
            "traceback": traceback.format_exc(),
        }


@mcp.tool()
def list_indexes() -> list[str]:
    """Lists available indexes."""
    if not os.path.exists(INDEXES_DIR):
        return []
    return os.listdir(INDEXES_DIR)


def _search_index(index_name: str, query: str, instruction_type: str, top_k: int = 10) -> dict[str, Any]:
    """Internal helper function to search an index with a specific instruction type.

    Args:
        index_name: Name of the index to search
        query: Query text (natural language or code)
        instruction_type: Type of instruction from INSTRUCTION_CONFIG (e.g., 'nl2code', 'code2code')
        top_k: Number of results to return

    Returns:
        Dictionary with search results or error information
    """
    try:
        index_path = os.path.join(INDEXES_DIR, index_name)
        faiss_index_path = os.path.join(index_path, "faiss.index")

        if not os.path.exists(faiss_index_path):
            return {"error": f"Index '{index_name}' not found"}

        # Load FAISS index
        index = faiss.read_index(faiss_index_path)

        # Generate query embedding with appropriate prefix
        prefixed_query = INSTRUCTION_CONFIG[instruction_type]["query"] + query
        query_embedding = get_embedding(prefixed_query)
        query_vector = query_embedding.reshape(1, -1).astype("float32")

        # Debug: check dimensions
        if query_vector.shape[1] != index.d:
            return {
                "error": f"Dimension mismatch: query has {query_vector.shape[1]} dimensions, index has {index.d} dimensions",
                "query_shape": str(query_vector.shape),
                "index_dimension": index.d,
                "query_embedding_shape": str(query_embedding.shape),
            }

        # Search the index
        # FAISS returns (distances/scores, indices/ids)
        scores, ids = index.search(query_vector, top_k)
        return {"ids": ids.tolist(), "scores": scores.tolist()}
    except Exception as e:
        return {
            "error": f"Error searching index: {str(e)}",
            "traceback": traceback.format_exc(),
        }


def _search(index_name: str, query: str, top_k: int = 10) -> dict[str, Any]:
    """Undecorated search function for internal/external use.

    Searches a semantic index for code matching a natural language description.
    """
    return _search_index(index_name, query, "nl2code", top_k)


def _search_code(index_name: str, code_query: str, top_k: int = 10) -> dict[str, Any]:
    """Undecorated search_code function for internal/external use.

    Searches a semantic index for code similar to the provided code snippet.
    """
    return _search_index(index_name, code_query, "code2code", top_k)


def _get_chunk(index_name: str, chunk_id: int) -> dict:
    """Undecorated get_chunk function for internal/external use.

    Retrieves the content and metadata of a specific chunk by its ID.
    """
    try:
        import json

        index_path = os.path.join(INDEXES_DIR, index_name)
        metadata_path = os.path.join(index_path, "chunks_metadata.json")

        if not os.path.exists(metadata_path):
            return {"error": f"Metadata file not found for index '{index_name}'"}

        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        if chunk_id < 0 or chunk_id >= len(metadata):
            return {
                "error": f"Chunk ID {chunk_id} out of range (0-{len(metadata) - 1})"
            }

        chunk_meta = metadata[chunk_id]
        filepath = chunk_meta["filepath"]
        start_line = chunk_meta["startLine"]
        end_line = chunk_meta["endLine"]

        # Determine the clone path from the index name
        clone_path = None

        index_config_path = os.path.join(index_path, "index_config.json")
        if os.path.exists(index_config_path):
            with open(index_config_path, "r", encoding="utf-8") as f:
                index_config = json.load(f)
                clone_path = index_config.get("original_repo_path")

        # If clone_path is still None, it means either index_config.json didn't exist
        # or didn't contain original_repo_path, so we fall back to CLONES_DIR logic
        if clone_path is None:
            for dir_name in os.listdir(CLONES_DIR):
                if index_name.startswith(dir_name.rsplit("-", 1)[0]):
                    clone_path = os.path.join(CLONES_DIR, dir_name)
                    break

        # If not found in clones, might be a local path index
        if clone_path is None or not os.path.exists(clone_path):
            # Try to find the original path from indexing jobs
            # For now, just return error
            return {
                "error": f"Source repository not found for index '{index_name}'",
                "filepath": filepath,
                "startLine": start_line,
                "endLine": end_line,
            }

        full_path = os.path.join(clone_path, filepath)

        # Read the specific bytes from the file using seek
        start_byte = chunk_meta["startByte"]
        end_byte = chunk_meta["endByte"]

        with open(full_path, "rb") as f:
            f.seek(start_byte)
            content_bytes = f.read(end_byte - start_byte)
            content = content_bytes.decode("utf-8")

        return {
            "filepath": filepath,
            "startLine": start_line,
            "endLine": end_line,
            "content": content,
        }
    except Exception as e:
        return {
            "error": f"Error getting chunk: {str(e)}",
            "traceback": traceback.format_exc(),
        }


@mcp.tool()
def search(index_name: str, query: str, top_k: int = 10) -> dict[str, Any]:
    """Advanced alternative to regular expressions and file pattern matching. Searches a semantic index for code matching a natural language description, e.g.:

    'Find a function that prints a greeting message to the console.'

    Returns the top_k most relevant chunks with their scores and IDs."""
    return _search(index_name, query, top_k)


@mcp.tool()
def search_code(index_name: str, code_query: str, top_k: int = 10) -> dict[str, Any]:
    """Advanced alternative to regular expressions and exact search. Searches a semantic index for code similar to the provided code snippet, e.g.:

    'def hello(): print("Hello")'

    Returns the top_k most relevant chunks with their scores and IDs."""
    return _search_code(index_name, code_query, top_k)


@mcp.tool()
def get_chunk(index_name: str, chunk_id: int) -> dict:
    """Retrieves the content and metadata of a specific chunk by its ID.

    Args:
        index_name: Name of the index to retrieve from
        chunk_id: The ID of the chunk to retrieve (as returned by search results)

    Returns:
        JSON string with filepath, startLine, endLine, and content
    """
    return _get_chunk(index_name, chunk_id)


def main():
    # Ensure necessary directories exist
    os.makedirs(INDEXES_DIR, exist_ok=True)
    os.makedirs(CLONES_DIR, exist_ok=True)
    mcp.run(transport="http", port=8888)


if __name__ == "__main__":
    main()
