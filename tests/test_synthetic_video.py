"""Tests for the generate_synthetic_video MCP tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openfilter_mcp.synthetic_video import (
    generate_synthetic_video,
    poll_until_complete,
)

# Default payload that's always included in requests
DEFAULT_PAYLOAD = {
    "frame_count": 100,
    "resolution": {"width": 1280, "height": 720},
}


class TestPollUntilComplete:
    """Tests for poll_until_complete helper function."""

    @pytest.mark.asyncio
    async def test_returns_immediately_on_completed_status(self):
        """Should return job immediately when status is completed."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "test-job-id",
            "status": "completed",
            "output_video_id": "video-123",
            "output_gcs_uri": "gs://bucket/video.mp4",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await poll_until_complete(
                "project-123", "test-job-id", poll_interval=0.1
            )

        assert result["status"] == "completed"
        assert result["output_video_id"] == "video-123"
        assert result["output_gcs_uri"] == "gs://bucket/video.mp4"
        mock_client.get.assert_called_once_with(
            "/projects/project-123/synthetic-videos/test-job-id"
        )

    @pytest.mark.asyncio
    async def test_returns_on_failed_status(self):
        """Should return job when status is failed."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "test-job-id",
            "status": "failed",
            "error_message": "Generation failed",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await poll_until_complete(
                "project-123", "test-job-id", poll_interval=0.1
            )

        assert result["status"] == "failed"
        assert result["error_message"] == "Generation failed"

    @pytest.mark.asyncio
    async def test_polls_until_complete(self):
        """Should poll multiple times until job completes."""
        responses = [
            {"id": "test-job-id", "status": "queued"},
            {"id": "test-job-id", "status": "processing"},
            {
                "id": "test-job-id",
                "status": "completed",
                "output_video_id": "vid-1",
                "output_gcs_uri": "gs://bucket/vid-1.mp4",
            },
        ]
        call_count = 0

        def make_response():
            nonlocal call_count
            mock_response = MagicMock()
            mock_response.json.return_value = responses[call_count]
            mock_response.raise_for_status = MagicMock()
            call_count += 1
            return mock_response

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=lambda _: make_response())
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await poll_until_complete(
                "project-123", "test-job-id", poll_interval=0.01
            )

        assert result["status"] == "completed"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_timeout_error_on_max_wait(self):
        """Should raise TimeoutError when max_wait is exceeded."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "test-job-id",
            "status": "processing",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            with pytest.raises(TimeoutError) as exc_info:
                await poll_until_complete(
                    "project-123",
                    "test-job-id",
                    poll_interval=0.01,
                    max_wait=0.025,
                )

        assert "did not complete within" in str(exc_info.value)
        assert "processing" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handles_case_insensitive_status(self):
        """Should handle different case variations of status."""
        for status in ["COMPLETED", "Complete", "DONE", "Done"]:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "id": "test-job-id",
                "status": status,
            }
            mock_response.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "openfilter_mcp.synthetic_video.async_api_client",
                return_value=mock_client,
            ):
                result = await poll_until_complete(
                    "project-123", "test-job-id", poll_interval=0.1
                )

            assert result["status"] == status


class TestGenerateSyntheticVideo:
    """Tests for generate_synthetic_video MCP tool."""

    @pytest.mark.asyncio
    async def test_creates_job_with_prompt(self):
        """Should create job with prompt and required fields."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "new-job-id",
            "status": "queued",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await generate_synthetic_video(
                project_id="project-123",
                prompt="A car driving on a sunny road",
            )

        assert result["id"] == "new-job-id"
        assert result["status"] == "queued"
        mock_client.post.assert_called_once_with(
            "/projects/project-123/synthetic-videos",
            json={**DEFAULT_PAYLOAD, "project_id": "project-123", "prompt": "A car driving on a sunny road"},
        )

    @pytest.mark.asyncio
    async def test_creates_job_with_seed_video_and_filters(self):
        """Should create job with seed_video_id and filters."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "filter-job-id",
            "status": "queued",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        filters = [
            {"filter_id": "mirror", "parameters": {}},
            {"filter_id": "brightness", "parameters": {"value": 1.2}},
        ]

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await generate_synthetic_video(
                project_id="project-456",
                seed_video_id="existing-video-id",
                filters=filters,
            )

        assert result["id"] == "filter-job-id"
        mock_client.post.assert_called_once_with(
            "/projects/project-456/synthetic-videos",
            json={
                **DEFAULT_PAYLOAD,
                "project_id": "project-456",
                "seed_video_id": "existing-video-id",
                "filters": filters,
            },
        )

    @pytest.mark.asyncio
    async def test_creates_job_with_custom_resolution(self):
        """Should create job with custom resolution."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "custom-job-id",
            "status": "queued",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await generate_synthetic_video(
                project_id="project-789",
                frame_count=500,
                resolution_width=1920,
                resolution_height=1080,
                prompt="HD video test",
            )

        mock_client.post.assert_called_once_with(
            "/projects/project-789/synthetic-videos",
            json={
                "project_id": "project-789",
                "frame_count": 500,
                "resolution": {"width": 1920, "height": 1080},
                "prompt": "HD video test",
            },
        )

    @pytest.mark.asyncio
    async def test_creates_job_with_defaults_only(self):
        """Should create job with default required fields when no optional params given."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "default-job-id",
            "status": "queued",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await generate_synthetic_video(project_id="project-default")

        mock_client.post.assert_called_once_with(
            "/projects/project-default/synthetic-videos",
            json={**DEFAULT_PAYLOAD, "project_id": "project-default"},
        )

    @pytest.mark.asyncio
    async def test_waits_for_completion_when_requested(self):
        """Should poll until completion when wait_for_completion is True."""
        create_response = MagicMock()
        create_response.json.return_value = {
            "id": "wait-job-id",
            "status": "queued",
        }
        create_response.raise_for_status = MagicMock()

        poll_response = MagicMock()
        poll_response.json.return_value = {
            "id": "wait-job-id",
            "status": "completed",
            "output_video_id": "generated-video-id",
            "output_gcs_uri": "gs://bucket/generated.mp4",
        }
        poll_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_response)
        mock_client.get = AsyncMock(return_value=poll_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await generate_synthetic_video(
                project_id="project-wait",
                prompt="Generate video",
                wait_for_completion=True,
            )

        assert result["status"] == "completed"
        assert result["output_video_id"] == "generated-video-id"
        assert result["output_gcs_uri"] == "gs://bucket/generated.mp4"
        mock_client.post.assert_called_once()
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_wait_when_flag_is_false(self):
        """Should return immediately when wait_for_completion is False."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "nowait-job-id",
            "status": "queued",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            result = await generate_synthetic_video(
                project_id="project-nowait",
                prompt="Generate video",
                wait_for_completion=False,
            )

        assert result["status"] == "queued"
        mock_client.post.assert_called_once()
        mock_client.get.assert_not_called()

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
            "openfilter_mcp.synthetic_video.async_api_client", return_value=mock_client
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await generate_synthetic_video(
                    project_id="nonexistent-project",
                    prompt="Generate video",
                )
