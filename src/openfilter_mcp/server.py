import os
from fastmcp import FastMCP
from typing import Any, Dict
from pathlib import Path

from code_context.main import _search_index, _get_chunk
from code_context.indexing import INDEXES_DIR
from openfilter_mcp.preindex_repos import MONOREPO_CLONE_DIR  # Import MONOREPO_CLONE_DIR

mcp = FastMCP(name="OpenFilter MCP")


def get_latest_index_name() -> str:
    """Finds the most recently modified index name related to MONOREPO_CLONE_DIR."""
    try:
        latest_index_entry = max(
            (
                entry
                for entry in os.scandir(INDEXES_DIR)
                if entry.is_dir() and entry.name.startswith(MONOREPO_CLONE_DIR)
            ),
            key=lambda entry: entry.stat().st_mtime,
        )
        return latest_index_entry.name
    except ValueError:
        raise FileNotFoundError(
            f"No index found for {MONOREPO_CLONE_DIR} in {INDEXES_DIR}"
        )


LATEST_INDEX_NAME = get_latest_index_name()


@mcp.tool()
def search(query: str, top_k: int = 10) -> Dict[str, Any]:
    """Searches a semantic index for code matching a natural language description.

    Returns the top_k most relevant chunks with their scores and metadata."""
    return _search_index(LATEST_INDEX_NAME, query, "nl2code", top_k)


@mcp.tool()
def search_code(code_query: str, top_k: int = 10) -> Dict[str, Any]:
    """Searches a semantic index for code similar to the provided code snippet.

    Returns the top_k most relevant chunks with their scores and metadata."""
    return _search_index(LATEST_INDEX_NAME, code_query, "code2code", top_k)


@mcp.tool()
def get_chunk(chunk_id: int) -> Dict[str, Any]:
    """Retrieves the content and metadata of a specific chunk by its ID.

    Returns: JSON object with filepath, startLine, endLine, and content."""
    return _get_chunk(LATEST_INDEX_NAME, chunk_id)


def _is_subpath(path, parent_directory):
    path = os.path.realpath(path)
    parent_directory = os.path.realpath(parent_directory)
    return path.startswith(parent_directory + os.sep)


def _real_path(path):
    "Check that a path resolution is secure and valid"
    path = os.path.join(MONOREPO_CLONE_DIR, path)
    if _is_subpath(path, MONOREPO_CLONE_DIR):
        return path
    else:
        raise FileNotFoundError("Path is not within the monorepo directory.")


@mcp.tool()
def read_file(filepath: str, start_line: int = 0, line_count: int = 100) -> str:
    """Reads the content of a virtual file in the monorepo index.

    Returns: The content of the file as a string."""
    with open(_real_path(filepath), "r") as file:
        content = file.read()
    lines = content.splitlines()
    content = "\n".join(lines[start_line : start_line + line_count])
    return content


def main():
    # Ensure necessary directories exist (these are also handled by code-context, but good to have)
    os.makedirs(INDEXES_DIR, exist_ok=True)
    mcp.run(transport="http", port=3000, host="0.0.0.0")


if __name__ == "__main__":
    main()
