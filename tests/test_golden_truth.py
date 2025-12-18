"""Functional tests for the add_golden_truth MCP tool.

These tests validate the golden truth API integration with both
unit tests (mocked) and optional functional tests (live API).
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openfilter_mcp.auth import read_psctl_token
from openfilter_mcp.golden_truth import add_golden_truth


class TestAddGoldenTruthUnit:
    """Unit tests for add_golden_truth with mocked API."""

    @pytest.mark.asyncio
    async def test_creates_golden_truth_with_required_fields(self):
        """Should create golden truth with test_id, video_id, and annotations."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "golden-truth-123",
            "test_id": "test-456",
            "video_id": "video-789",
            "annotations": {"labels": ["person", "car"]},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.golden_truth.async_api_client", return_value=mock_client
        ):
            result = await add_golden_truth(
                test_id="test-456",
                video_id="video-789",
                annotations={"labels": ["person", "car"]},
            )

        assert result["id"] == "golden-truth-123"
        assert result["test_id"] == "test-456"
        assert result["video_id"] == "video-789"
        mock_client.post.assert_called_once_with(
            "/tests/test-456/golden-truth-files",
            json={
                "video_id": "video-789",
                "annotations": {"labels": ["person", "car"]},
            },
        )

    @pytest.mark.asyncio
    async def test_creates_golden_truth_with_description(self):
        """Should include description in payload when provided."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "golden-truth-with-desc",
            "test_id": "test-456",
            "video_id": "video-789",
            "annotations": {"bboxes": []},
            "description": "Ground truth for parking lot scene",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.golden_truth.async_api_client", return_value=mock_client
        ):
            result = await add_golden_truth(
                test_id="test-456",
                video_id="video-789",
                annotations={"bboxes": []},
                description="Ground truth for parking lot scene",
            )

        assert result["description"] == "Ground truth for parking lot scene"
        mock_client.post.assert_called_once_with(
            "/tests/test-456/golden-truth-files",
            json={
                "video_id": "video-789",
                "annotations": {"bboxes": []},
                "description": "Ground truth for parking lot scene",
            },
        )

    @pytest.mark.asyncio
    async def test_omits_description_when_none(self):
        """Should not include description in payload when None."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "gt-no-desc"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.golden_truth.async_api_client", return_value=mock_client
        ):
            await add_golden_truth(
                test_id="test-123",
                video_id="video-456",
                annotations={},
                description=None,
            )

        # Verify description is not in the payload
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert "description" not in payload

    @pytest.mark.asyncio
    async def test_handles_complex_annotations(self):
        """Should handle complex annotation structures with frames and bboxes."""
        complex_annotations = {
            "frames": [
                {
                    "frame_number": 0,
                    "objects": [
                        {
                            "label": "person",
                            "bbox": [100, 200, 150, 300],
                            "confidence": 1.0,
                        },
                        {
                            "label": "car",
                            "bbox": [400, 300, 600, 500],
                            "confidence": 1.0,
                        },
                    ],
                }
            ],
            "metadata": {
                "annotator": "human",
                "version": "1.0",
            },
        }

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "complex-gt",
            "annotations": complex_annotations,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.golden_truth.async_api_client", return_value=mock_client
        ):
            result = await add_golden_truth(
                test_id="test-complex",
                video_id="video-complex",
                annotations=complex_annotations,
            )

        assert result["annotations"] == complex_annotations
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["annotations"] == complex_annotations

    @pytest.mark.asyncio
    async def test_propagates_http_errors(self):
        """Should propagate HTTP errors from the API."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.golden_truth.async_api_client", return_value=mock_client
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await add_golden_truth(
                    test_id="nonexistent-test",
                    video_id="video-123",
                    annotations={},
                )

    @pytest.mark.asyncio
    async def test_propagates_validation_errors(self):
        """Should propagate validation errors (400) from the API."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Request",
            request=MagicMock(),
            response=MagicMock(status_code=400),
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.golden_truth.async_api_client", return_value=mock_client
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await add_golden_truth(
                    test_id="test-123",
                    video_id="invalid-video",
                    annotations={"invalid": "format"},
                )

    @pytest.mark.asyncio
    async def test_uses_correct_endpoint_format(self):
        """Should use the correct API endpoint format."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "gt-endpoint-test"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        test_id = "my-test-uuid-12345"

        with patch(
            "openfilter_mcp.golden_truth.async_api_client", return_value=mock_client
        ):
            await add_golden_truth(
                test_id=test_id,
                video_id="video-id",
                annotations={},
            )

        # Verify the endpoint uses the correct format
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        endpoint = call_args[0][0]
        assert endpoint == f"/tests/{test_id}/golden-truth-files"


# Functional tests that require live API
@pytest.mark.skipif(
    read_psctl_token() is None and not os.getenv("PLAINSIGHT_API_TOKEN"),
    reason="No authentication token available (run psctl login or set PLAINSIGHT_API_TOKEN)",
)
class TestAddGoldenTruthFunctional:
    """Functional tests that make real API calls.

    These tests require:
    - Valid authentication via psctl login or PLAINSIGHT_API_TOKEN env var
    - TEST_PROJECT_ID environment variable set to a valid project
    - TEST_TEST_ID environment variable set to a valid test
    - TEST_VIDEO_ID environment variable set to a valid video in the corpus
    """

    @pytest.fixture
    def test_id(self):
        """Get test ID from environment."""
        test_id = os.getenv("TEST_TEST_ID")
        if not test_id:
            pytest.skip("TEST_TEST_ID environment variable not set")
        return test_id

    @pytest.fixture
    def video_id(self):
        """Get video ID from environment."""
        video_id = os.getenv("TEST_VIDEO_ID")
        if not video_id:
            pytest.skip("TEST_VIDEO_ID environment variable not set")
        return video_id

    @pytest.mark.asyncio
    async def test_add_golden_truth_live(self, test_id, video_id):
        """Functional test: add golden truth to the live API."""
        result = await add_golden_truth(
            test_id=test_id,
            video_id=video_id,
            annotations={
                "labels": ["test_label"],
                "metadata": {"source": "functional_test"},
            },
            description="Functional test golden truth",
        )

        # Verify response structure
        assert "id" in result
        assert result["id"]  # Should be non-empty

        print(f"Successfully created golden truth: {result.get('id')}")
