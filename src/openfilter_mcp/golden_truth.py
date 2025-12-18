"""Golden truth management for OpenFilter MCP.

This module provides functions for managing golden truth files associated with tests.
"""

from typing import Any, Dict, Optional

from openfilter_mcp.auth import async_api_client


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
    payload: Dict[str, Any] = {
        "test_id": test_id,
        "video_file_reference": video_file_reference,
        "storage_path": storage_path,
    }
    if metadata is not None:
        payload["metadata"] = metadata

    async with async_api_client() as client:
        response = await client.post(
            f"/tests/{test_id}/golden-truth-files",
            json=payload,
        )
        response.raise_for_status()
        return response.json()
