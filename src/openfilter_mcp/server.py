import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_context.indexing import INDEXES_DIR
from code_context.main import _get_chunk, _search_index
from fastmcp import FastMCP

from openfilter_mcp.auth import create_token_verifier
from openfilter_mcp.preindex_repos import (
    MONOREPO_CLONE_DIR,  # Import MONOREPO_CLONE_DIR
)
from openfilter_mcp.synthetic_video import (
    generate_synthetic_video as _generate_synthetic_video,
)

# Create MCP server with bearer token authentication
# Tokens are passed through to plainsight-api for validation
mcp = FastMCP(name="OpenFilter MCP", auth=create_token_verifier())


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


@mcp.tool()
async def generate_synthetic_video(
    project_id: str,
    frame_count: int = 100,
    resolution_width: int = 1280,
    resolution_height: int = 720,
    prompt: Optional[str] = None,
    seed_video_id: Optional[str] = None,
    filters: Optional[List[Dict[str, Any]]] = None,
    wait_for_completion: bool = False,
) -> Dict[str, Any]:
    """Generate a synthetic test video using AI or filter transforms.

    Creates a synthetic video generation job. The video can be generated from
    a natural language prompt, by applying filters to an existing seed video,
    or both. Output format is always MP4 (determined by Veo API).

    Args:
        project_id: Target project UUID.
        frame_count: Target number of frames (1-10000). Defaults to 100.
        resolution_width: Output video width in pixels. Defaults to 1280.
        resolution_height: Output video height in pixels. Defaults to 720.
        prompt: Natural language description for AI video generation.
        seed_video_id: Existing video UUID to apply transforms to.
        filters: List of filter configs, e.g. [{"filter_id": "mirror", "parameters": {}}].
        wait_for_completion: If true, poll until the job completes.

    Returns:
        Job details dict including:
        - id: The UUID of the created job
        - status: Current job status (queued, processing, completed, failed)
        - output_video_id: The generated video UUID (when completed)
        - output_gcs_uri: GCS URI for the generated video (when completed)
        - error_message: Error details (if failed)
    """
    return await _generate_synthetic_video(
        project_id=project_id,
        frame_count=frame_count,
        resolution_width=resolution_width,
        resolution_height=resolution_height,
        prompt=prompt,
        seed_video_id=seed_video_id,
        filters=filters,
        wait_for_completion=wait_for_completion,
    )


def main():
    # Ensure necessary directories exist (these are also handled by code-context, but good to have)
    os.makedirs(INDEXES_DIR, exist_ok=True)
    mcp.run(transport="http", port=3000, host="0.0.0.0")


if __name__ == "__main__":
    main()
