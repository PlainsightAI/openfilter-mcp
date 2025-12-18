"""OpenFilter MCP Server.

Provides tools for interacting with the Plainsight API for:
- Code semantic search
- Video corpus management
- Test management
- Synthetic video generation
"""

import asyncio
import os
from typing import Any, Dict, List, Optional

from code_context.indexing import INDEXES_DIR
from code_context.main import _get_chunk, _search_index
from fastmcp import FastMCP

from openfilter_mcp.auth import async_api_client
from openfilter_mcp.golden_truth import add_golden_truth as _add_golden_truth
from openfilter_mcp.preindex_repos import MONOREPO_CLONE_DIR
from openfilter_mcp.synthetic_video import (
    generate_synthetic_video as _generate_synthetic_video,
    poll_until_complete as _poll_synthetic_video_job,
)

# Create MCP server
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


# =============================================================================
# Code Search Tools
# =============================================================================


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
    """Check that a path resolution is secure and valid."""
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


# =============================================================================
# Video Corpus Tools
# =============================================================================


@mcp.tool()
async def list_video_corpus(
    project_id: str,
    source: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """List videos in the corpus for a project.

    Args:
        project_id: The ID of the project to list videos for.
        source: Optional filter by source type (uploaded, recorded, synthetic).
        limit: Maximum number of videos to return (default 50).
        offset: Number of videos to skip for pagination (default 0).

    Returns:
        Dictionary containing the list of videos and pagination info.
    """
    params: Dict[str, Any] = {
        "limit": limit,
        "offset": offset,
    }
    if source is not None:
        params["source"] = source

    async with async_api_client() as client:
        response = await client.get(
            f"/projects/{project_id}/videos",
            params=params,
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def upload_video(
    project_id: str,
    file_path: str,
    title: str,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload a video file to the project's video corpus.

    Args:
        project_id: The project ID to upload to.
        file_path: Local path to the video file.
        title: Title for the uploaded video.
        description: Optional description.

    Returns:
        The created video object with id, gcs_uri, etc.
    """
    import os as os_module

    if not os_module.path.exists(file_path):
        raise FileNotFoundError(f"Video file not found: {file_path}")

    filename = os_module.path.basename(file_path)

    async with async_api_client() as client:
        with open(file_path, "rb") as video_file:
            files = {"file": (filename, video_file, "video/mp4")}
            data: Dict[str, str] = {"title": title}
            if description is not None:
                data["description"] = description

            response = await client.post(
                f"/projects/{project_id}/videos/upload",
                files=files,
                data=data,
            )
            response.raise_for_status()
            return response.json()


@mcp.tool()
async def get_video(
    project_id: str,
    video_id: str,
    include_download_url: bool = False,
) -> Dict[str, Any]:
    """Get video details and optionally a signed download URL.

    Args:
        project_id: The project ID containing the video.
        video_id: The ID of the video to retrieve.
        include_download_url: If True, includes a signed download URL in the response.

    Returns:
        JSON object with video metadata (id, name, status, duration, etc.)
        and optionally a signed download_url field.
    """
    async with async_api_client() as client:
        response = await client.get(
            f"/projects/{project_id}/videos/{video_id}"
        )
        response.raise_for_status()
        video_data = response.json()

        if include_download_url:
            download_response = await client.get(
                f"/projects/{project_id}/videos/{video_id}/download"
            )
            download_response.raise_for_status()
            download_data = download_response.json()
            video_data["download_url"] = download_data.get("url")

        return video_data


# =============================================================================
# Test Management Tools
# =============================================================================


@mcp.tool()
async def list_tests(
    project_id: Optional[str] = None,
    filter_pipeline_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """List test definitions with optional filtering.

    Args:
        project_id: Optional project ID to filter tests by.
        filter_pipeline_id: Optional filter pipeline ID to filter tests by.
        limit: Maximum number of tests to return (default: 50).
        offset: Number of tests to skip for pagination (default: 0).

    Returns:
        A list of test objects.
    """
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if project_id is not None:
        params["project_id"] = project_id
    if filter_pipeline_id is not None:
        params["filter_pipeline_id"] = filter_pipeline_id

    async with async_api_client() as client:
        response = await client.get(
            "/tests",
            params=params,
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def create_test(
    project_id: str,
    filter_pipeline_id: str,
    name: str,
    description: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a test definition for a filter pipeline.

    Args:
        project_id: The ID of the project this test belongs to.
        filter_pipeline_id: The ID of the filter pipeline to test.
        name: Name of the test.
        description: Optional description of the test.
        metadata: Optional metadata for the test.

    Returns:
        The created test object.
    """
    payload: Dict[str, Any] = {
        "project_id": project_id,
        "filter_pipeline_id": filter_pipeline_id,
        "name": name,
    }
    if description is not None:
        payload["description"] = description
    if metadata is not None:
        payload["metadata"] = metadata

    async with async_api_client() as client:
        response = await client.post(
            "/tests",
            json=payload,
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_test(
    test_id: str,
) -> Dict[str, Any]:
    """Get details of a specific test.

    Args:
        test_id: The ID of the test to retrieve.

    Returns:
        The test object with its assertions and golden truth files.
    """
    async with async_api_client() as client:
        response = await client.get(
            f"/tests/{test_id}"
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def add_assertion(
    test_id: str,
    name: str,
    assertion_type: str,
    assertion_config: Dict[str, Any],
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Add an assertion to a test.

    Args:
        test_id: The ID of the test.
        name: Name of the assertion.
        assertion_type: Type of assertion (e.g., 'count', 'presence', 'absence').
        assertion_config: Configuration for the assertion (e.g., {"label": "person", "operator": "gt", "value": 5}).
        description: Optional description of the assertion.

    Returns:
        The created assertion object.
    """
    payload: Dict[str, Any] = {
        "test_id": test_id,
        "name": name,
        "assertion_type": assertion_type,
        "assertion_config": assertion_config,
    }
    if description is not None:
        payload["description"] = description

    async with async_api_client() as client:
        response = await client.post(
            f"/tests/{test_id}/assertions",
            json=payload,
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def add_golden_truth(
    test_id: str,
    video_file_reference: str,
    storage_path: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Associate a golden truth file with a test.

    Args:
        test_id: The test ID to add golden truth to.
        video_file_reference: Reference to the video file.
        storage_path: Storage path where the golden truth file is stored.
        metadata: Optional metadata for the golden truth file.

    Returns:
        The created golden truth file object.
    """
    return await _add_golden_truth(test_id, video_file_reference, storage_path, metadata)


# =============================================================================
# Synthetic Video Generation Tools
# =============================================================================


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
    # Ensure necessary directories exist
    os.makedirs(INDEXES_DIR, exist_ok=True)
    mcp.run(transport="http", port=3000, host="0.0.0.0")


if __name__ == "__main__":
    main()
