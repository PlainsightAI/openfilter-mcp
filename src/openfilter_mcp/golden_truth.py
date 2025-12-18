"""Golden truth management for OpenFilter MCP.

This module provides functions for managing golden truth files associated with tests.
"""

from typing import Any, Dict, Optional

from openfilter_mcp.auth import async_api_client


async def add_golden_truth(
    test_id: str,
    video_id: str,
    annotations: Dict[str, Any],
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Associate a golden truth file with a test.

    Args:
        test_id: The test ID to add golden truth to.
        video_id: The video ID from the corpus that this golden truth is for.
        annotations: The ground truth annotations (labels, bounding boxes, etc.)
        description: Optional description.

    Returns:
        The created golden truth file object.
    """
    payload: Dict[str, Any] = {
        "video_id": video_id,
        "annotations": annotations,
    }
    if description is not None:
        payload["description"] = description

    async with async_api_client() as client:
        response = await client.post(
            f"/tests/{test_id}/golden-truth-files",
            json=payload,
        )
        response.raise_for_status()
        return response.json()
