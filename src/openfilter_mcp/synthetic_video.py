"""Synthetic video generation MCP tool.

This module provides the generate_synthetic_video tool for creating
synthetic test videos using AI or filter transforms.
"""

import asyncio
from typing import Any, Dict, List, Optional

from openfilter_mcp.auth import async_api_client


async def poll_until_complete(
    project_id: str,
    job_id: str,
    poll_interval: float = 30.0,
    max_wait: float = 600.0,
) -> Dict[str, Any]:
    """Poll a synthetic video job until it completes or times out.

    Args:
        project_id: The project UUID.
        job_id: The job UUID to poll.
        poll_interval: Seconds between poll requests.
        max_wait: Maximum seconds to wait before timing out.

    Returns:
        The final job status dict.

    Raises:
        TimeoutError: If max_wait is exceeded.
        httpx.HTTPStatusError: If API request fails.
    """
    elapsed = 0.0
    while elapsed < max_wait:
        async with async_api_client() as client:
            response = await client.get(
                f"/projects/{project_id}/synthetic-videos/{job_id}"
            )
            response.raise_for_status()
            job = response.json()

        status = job.get("status", "").lower()
        if status in ("completed", "complete", "done", "failed", "error"):
            return job

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(
        f"Job {job_id} did not complete within {max_wait} seconds. "
        f"Last status: {job.get('status')}"
    )


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
    payload: Dict[str, Any] = {
        "project_id": project_id,
        "frame_count": frame_count,
        "resolution": {
            "width": resolution_width,
            "height": resolution_height,
        },
    }
    if prompt is not None:
        payload["prompt"] = prompt
    if seed_video_id is not None:
        payload["seed_video_id"] = seed_video_id
    if filters is not None:
        payload["filters"] = filters

    async with async_api_client() as client:
        response = await client.post(
            f"/projects/{project_id}/synthetic-videos",
            json=payload,
        )
        response.raise_for_status()
        job = response.json()

    if wait_for_completion:
        job = await poll_until_complete(project_id, job["id"])

    return job
