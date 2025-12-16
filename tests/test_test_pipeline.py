"""
Unit tests for the test_pipeline module.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from openfilter_mcp.test_pipeline import (
    DEFAULT_API_BASE_URL,
    DEFAULT_POLL_INTERVAL,
    PipelineTestClient,
    _is_terminal_status,
    poll_until_complete,
    run_test_against_pipeline,
)


class TestIsTerminalStatus:
    """Tests for the _is_terminal_status function."""

    def test_completed_is_terminal(self):
        assert _is_terminal_status("completed") is True
        assert _is_terminal_status("COMPLETED") is True

    def test_failed_is_terminal(self):
        assert _is_terminal_status("failed") is True
        assert _is_terminal_status("FAILED") is True

    def test_cancelled_is_terminal(self):
        assert _is_terminal_status("cancelled") is True

    def test_error_is_terminal(self):
        assert _is_terminal_status("error") is True

    def test_pending_is_not_terminal(self):
        assert _is_terminal_status("pending") is False

    def test_running_is_not_terminal(self):
        assert _is_terminal_status("running") is False

    def test_unknown_is_not_terminal(self):
        assert _is_terminal_status("unknown") is False


class TestPipelineTestClient:
    """Tests for the PipelineTestClient class."""

    def test_initialization_defaults(self):
        client = PipelineTestClient()
        assert client.base_url == DEFAULT_API_BASE_URL
        assert client.timeout == 30.0

    def test_initialization_custom_values(self):
        client = PipelineTestClient(
            base_url="https://custom.api.com/",
            timeout=60.0,
            api_key="test-key",
        )
        assert client.base_url == "https://custom.api.com"
        assert client.timeout == 60.0
        assert client.api_key == "test-key"

    def test_get_headers_without_api_key(self):
        client = PipelineTestClient(api_key=None)
        # Force api_key to None to avoid env var
        client.api_key = None
        headers = client._get_headers()
        assert "Content-Type" in headers
        assert headers["Content-Type"] == "application/json"
        assert "Authorization" not in headers

    def test_get_headers_with_api_key(self):
        client = PipelineTestClient(api_key="test-key-123")
        headers = client._get_headers()
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer test-key-123"

    @patch("httpx.Client")
    def test_create_test_run_minimal(self, mock_client_class):
        """Test creating a test run with minimal parameters."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "run_id": "run-123",
            "status": "pending",
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_class.return_value = mock_client

        client = PipelineTestClient(base_url="https://api.test.com")
        result = client.create_test_run(
            project_id="proj-123",
            pipeline_id="pipe-456",
            test_video_id="video-789",
        )

        assert result["run_id"] == "run-123"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "proj-123" in call_args[0][0]
        assert call_args[1]["json"]["pipeline_id"] == "pipe-456"
        assert call_args[1]["json"]["test_video_id"] == "video-789"
        assert "golden_truth_id" not in call_args[1]["json"]

    @patch("httpx.Client")
    def test_create_test_run_with_golden_truth(self, mock_client_class):
        """Test creating a test run with golden truth."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "run_id": "run-123",
            "status": "pending",
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_class.return_value = mock_client

        client = PipelineTestClient(base_url="https://api.test.com")
        result = client.create_test_run(
            project_id="proj-123",
            pipeline_id="pipe-456",
            test_video_id="video-789",
            golden_truth_id="truth-abc",
        )

        call_args = mock_client.post.call_args
        assert call_args[1]["json"]["golden_truth_id"] == "truth-abc"

    @patch("httpx.Client")
    def test_get_test_run_status(self, mock_client_class):
        """Test getting test run status."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "run_id": "run-123",
            "status": "running",
            "progress": 0.5,
        }
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_class.return_value = mock_client

        client = PipelineTestClient(base_url="https://api.test.com")
        result = client.get_test_run_status(
            project_id="proj-123",
            run_id="run-123",
        )

        assert result["status"] == "running"
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert "proj-123" in call_args[0][0]
        assert "run-123" in call_args[0][0]


class TestPollUntilComplete:
    """Tests for the poll_until_complete function."""

    @pytest.mark.asyncio
    async def test_immediate_completion(self):
        """Test polling when run is already complete."""
        mock_client = MagicMock(spec=PipelineTestClient)
        mock_client.get_test_run_status.return_value = {
            "run_id": "run-123",
            "status": "completed",
            "comparison_results": {"precision": 0.95},
        }

        result = await poll_until_complete(
            client=mock_client,
            project_id="proj-123",
            run_id="run-123",
            poll_interval=0.01,
        )

        assert result["status"] == "completed"
        mock_client.get_test_run_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_polls_until_completion(self):
        """Test that polling continues until completion."""
        mock_client = MagicMock(spec=PipelineTestClient)
        mock_client.get_test_run_status.side_effect = [
            {"run_id": "run-123", "status": "pending"},
            {"run_id": "run-123", "status": "running"},
            {"run_id": "run-123", "status": "completed", "result": "success"},
        ]

        result = await poll_until_complete(
            client=mock_client,
            project_id="proj-123",
            run_id="run-123",
            poll_interval=0.01,
        )

        assert result["status"] == "completed"
        assert mock_client.get_test_run_status.call_count == 3

    @pytest.mark.asyncio
    async def test_handles_failed_status(self):
        """Test that failed status is recognized as terminal."""
        mock_client = MagicMock(spec=PipelineTestClient)
        mock_client.get_test_run_status.side_effect = [
            {"run_id": "run-123", "status": "running"},
            {"run_id": "run-123", "status": "failed", "error": "Pipeline error"},
        ]

        result = await poll_until_complete(
            client=mock_client,
            project_id="proj-123",
            run_id="run-123",
            poll_interval=0.01,
        )

        assert result["status"] == "failed"
        assert result["error"] == "Pipeline error"

    @pytest.mark.asyncio
    async def test_timeout_on_max_attempts(self):
        """Test that TimeoutError is raised after max attempts."""
        mock_client = MagicMock(spec=PipelineTestClient)
        mock_client.get_test_run_status.return_value = {
            "run_id": "run-123",
            "status": "running",
        }

        with pytest.raises(TimeoutError) as exc_info:
            await poll_until_complete(
                client=mock_client,
                project_id="proj-123",
                run_id="run-123",
                poll_interval=0.01,
                max_attempts=3,
            )

        assert "run-123" in str(exc_info.value)
        assert mock_client.get_test_run_status.call_count == 3


class TestRunTestAgainstPipeline:
    """Tests for the run_test_against_pipeline function."""

    @pytest.mark.asyncio
    async def test_validation_empty_project_id(self):
        """Test that empty project_id raises ValueError."""
        with pytest.raises(ValueError, match="project_id is required"):
            await run_test_against_pipeline(
                project_id="",
                pipeline_id="pipe-456",
                test_video_id="video-789",
            )

    @pytest.mark.asyncio
    async def test_validation_empty_pipeline_id(self):
        """Test that empty pipeline_id raises ValueError."""
        with pytest.raises(ValueError, match="pipeline_id is required"):
            await run_test_against_pipeline(
                project_id="proj-123",
                pipeline_id="   ",
                test_video_id="video-789",
            )

    @pytest.mark.asyncio
    async def test_validation_empty_test_video_id(self):
        """Test that empty test_video_id raises ValueError."""
        with pytest.raises(ValueError, match="test_video_id is required"):
            await run_test_against_pipeline(
                project_id="proj-123",
                pipeline_id="pipe-456",
                test_video_id="",
            )

    @pytest.mark.asyncio
    @patch.object(PipelineTestClient, "create_test_run")
    async def test_no_wait_returns_immediately(self, mock_create):
        """Test that wait_for_completion=False returns immediately."""
        mock_create.return_value = {
            "run_id": "run-123",
            "status": "pending",
        }

        result = await run_test_against_pipeline(
            project_id="proj-123",
            pipeline_id="pipe-456",
            test_video_id="video-789",
            wait_for_completion=False,
        )

        assert result["run_id"] == "run-123"
        assert result["status"] == "pending"
        mock_create.assert_called_once()

    @pytest.mark.asyncio
    @patch.object(PipelineTestClient, "get_test_run_status")
    @patch.object(PipelineTestClient, "create_test_run")
    async def test_wait_polls_until_complete(self, mock_create, mock_status):
        """Test that wait_for_completion=True polls until done."""
        mock_create.return_value = {
            "run_id": "run-123",
            "status": "pending",
        }
        mock_status.side_effect = [
            {"run_id": "run-123", "status": "running"},
            {
                "run_id": "run-123",
                "status": "completed",
                "comparison_results": {
                    "precision": 0.95,
                    "recall": 0.92,
                    "f1": 0.93,
                    "iou": 0.88,
                },
            },
        ]

        result = await run_test_against_pipeline(
            project_id="proj-123",
            pipeline_id="pipe-456",
            test_video_id="video-789",
            golden_truth_id="truth-abc",
            wait_for_completion=True,
        )

        assert result["status"] == "completed"
        assert result["comparison_results"]["precision"] == 0.95
        assert mock_status.call_count == 2

    @pytest.mark.asyncio
    @patch.object(PipelineTestClient, "create_test_run")
    async def test_missing_run_id_raises_error(self, mock_create):
        """Test that missing run_id in response raises ValueError."""
        mock_create.return_value = {
            "status": "pending",
            # run_id is missing
        }

        with pytest.raises(ValueError, match="missing run_id"):
            await run_test_against_pipeline(
                project_id="proj-123",
                pipeline_id="pipe-456",
                test_video_id="video-789",
                wait_for_completion=True,
            )

    @pytest.mark.asyncio
    @patch.object(PipelineTestClient, "create_test_run")
    async def test_custom_api_url(self, mock_create):
        """Test that custom API URL is used."""
        mock_create.return_value = {
            "run_id": "run-123",
            "status": "pending",
        }

        await run_test_against_pipeline(
            project_id="proj-123",
            pipeline_id="pipe-456",
            test_video_id="video-789",
            wait_for_completion=False,
            api_base_url="https://custom.api.com",
        )

        mock_create.assert_called_once()
