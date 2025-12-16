"""
MCP tool for running tests against pipelines.

This module provides functionality to execute test videos through ML pipelines
and compare results against golden truth data.
"""

import asyncio
import os
import time
from typing import Any

import httpx

# Default API configuration
DEFAULT_API_BASE_URL = os.getenv("PLAINSIGHT_API_URL", "http://localhost:8080")
DEFAULT_API_TIMEOUT = float(os.getenv("PLAINSIGHT_API_TIMEOUT", "30.0"))
DEFAULT_POLL_INTERVAL = float(os.getenv("PLAINSIGHT_POLL_INTERVAL", "2.0"))
DEFAULT_MAX_POLL_ATTEMPTS = int(os.getenv("PLAINSIGHT_MAX_POLL_ATTEMPTS", "300"))


class PipelineTestClient:
    """Client for interacting with the Plainsight test pipeline API."""

    def __init__(
        self,
        base_url: str = DEFAULT_API_BASE_URL,
        timeout: float = DEFAULT_API_TIMEOUT,
        api_key: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key or os.getenv("PLAINSIGHT_API_KEY")

    def _get_headers(self) -> dict[str, str]:
        """Build request headers including authentication if configured."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def create_test_run(
        self,
        project_id: str,
        pipeline_id: str,
        test_video_id: str,
        golden_truth_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new test run.

        Args:
            project_id: UUID of the project
            pipeline_id: UUID of the pipeline to test
            test_video_id: UUID of the test video from the corpus
            golden_truth_id: Optional UUID of golden truth for comparison

        Returns:
            Test run response containing run_id and initial status
        """
        url = f"{self.base_url}/api/v1/projects/{project_id}/test-runs"
        payload = {
            "pipeline_id": pipeline_id,
            "test_video_id": test_video_id,
        }
        if golden_truth_id is not None:
            payload["golden_truth_id"] = golden_truth_id

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=payload, headers=self._get_headers())
            response.raise_for_status()
            return response.json()

    def get_test_run_status(
        self,
        project_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Get the current status of a test run.

        Args:
            project_id: UUID of the project
            run_id: UUID of the test run

        Returns:
            Test run status including progress and results if completed
        """
        url = f"{self.base_url}/api/v1/projects/{project_id}/test-runs/{run_id}"

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=self._get_headers())
            response.raise_for_status()
            return response.json()


def _is_terminal_status(status: str) -> bool:
    """Check if a status indicates the test run has finished."""
    return status.lower() in ("completed", "failed", "cancelled", "error")


async def poll_until_complete(
    client: PipelineTestClient,
    project_id: str,
    run_id: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    max_attempts: int = DEFAULT_MAX_POLL_ATTEMPTS,
) -> dict[str, Any]:
    """Poll the test run status until completion or failure.

    Args:
        client: PipelineTestClient instance
        project_id: UUID of the project
        run_id: UUID of the test run
        poll_interval: Seconds between status checks
        max_attempts: Maximum number of polling attempts

    Returns:
        Final test run result

    Raises:
        TimeoutError: If max_attempts exceeded
        httpx.HTTPStatusError: If API returns an error
    """
    attempts = 0

    while attempts < max_attempts:
        result = client.get_test_run_status(project_id, run_id)
        status = result.get("status", "unknown")

        if _is_terminal_status(status):
            return result

        attempts += 1
        await asyncio.sleep(poll_interval)

    raise TimeoutError(
        f"Test run {run_id} did not complete after {max_attempts} attempts "
        f"({max_attempts * poll_interval} seconds)"
    )


async def run_test_against_pipeline(
    project_id: str,
    pipeline_id: str,
    test_video_id: str,
    golden_truth_id: str | None = None,
    wait_for_completion: bool = True,
    api_base_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Run a test video through a pipeline and compare against golden truth.

    This function triggers a test run on the Plainsight API, optionally waiting
    for completion and returning metrics comparing pipeline output to golden truth.

    Args:
        project_id: UUID of the project containing the pipeline
        pipeline_id: UUID of the pipeline to test
        test_video_id: UUID of the test video from the corpus (TI-61)
        golden_truth_id: Optional UUID of golden truth for comparison (TI-52).
            If provided, comparison metrics will be calculated.
        wait_for_completion: If True, poll until the test run completes.
            If False, return immediately with the run_id for later polling.
        api_base_url: Override the default API base URL
        api_key: Override the API key from environment

    Returns:
        dict containing:
            - run_id: UUID of the test run
            - status: Current status (pending, running, completed, failed)
            - pipeline_output_video_id: UUID of the output video (when completed)
            - comparison_results: Metrics dict when golden_truth_id provided:
                - precision: float (0-1)
                - recall: float (0-1)
                - f1: float (0-1)
                - iou: float (0-1) - Intersection over Union
                - frame_results: list of per-frame comparison data

    Raises:
        httpx.HTTPStatusError: If the API returns an error response
        TimeoutError: If wait_for_completion=True and the run doesn't complete
        ValueError: If required parameters are invalid

    Example:
        >>> result = await run_test_against_pipeline(
        ...     project_id="proj-123",
        ...     pipeline_id="pipe-456",
        ...     test_video_id="video-789",
        ...     golden_truth_id="truth-abc",
        ... )
        >>> print(f"Precision: {result['comparison_results']['precision']}")
    """
    if not project_id or not project_id.strip():
        raise ValueError("project_id is required")
    if not pipeline_id or not pipeline_id.strip():
        raise ValueError("pipeline_id is required")
    if not test_video_id or not test_video_id.strip():
        raise ValueError("test_video_id is required")

    client = PipelineTestClient(
        base_url=api_base_url or DEFAULT_API_BASE_URL,
        api_key=api_key,
    )

    # Create the test run
    run = client.create_test_run(
        project_id=project_id,
        pipeline_id=pipeline_id,
        test_video_id=test_video_id,
        golden_truth_id=golden_truth_id,
    )

    if not wait_for_completion:
        return run

    # Poll until completion
    run_id = run.get("run_id")
    if not run_id:
        raise ValueError("API response missing run_id")

    return await poll_until_complete(client, project_id, run_id)
