"""Functional tests for the upload_video MCP tool.

These tests make real API calls and require:
- Valid authentication (psctl login or PLAINSIGHT_API_TOKEN env var)
- A valid project_id with video upload permissions
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# For unit tests
from openfilter_mcp.auth import read_psctl_token


def create_test_video_file(path: Path, size_bytes: int = 1024) -> None:
    """Create a minimal test video file (just random bytes for testing upload)."""
    with open(path, "wb") as f:
        f.write(b"\x00" * size_bytes)


@pytest.fixture(scope="module", autouse=True)
def setup_indexes_dir(tmp_path_factory):
    """Create indexes directory and mock index for server module import."""
    # Create the indexes directory structure that server.py expects
    indexes_dir = tmp_path_factory.mktemp("indexes")
    mock_index_dir = indexes_dir / "openfilter"
    mock_index_dir.mkdir()

    # Patch the environment before importing server
    with patch.dict(os.environ, {"INDEXES_DIR": str(indexes_dir)}):
        with patch("code_context.indexing.INDEXES_DIR", str(indexes_dir)):
            with patch(
                "openfilter_mcp.preindex_repos.MONOREPO_CLONE_DIR", "openfilter"
            ):
                yield


class TestUploadVideoUnit:
    """Unit tests for upload_video with mocked API."""

    @pytest.fixture
    def mock_server_imports(self, tmp_path):
        """Mock the server module dependencies to allow import."""
        indexes_dir = tmp_path / "indexes"
        indexes_dir.mkdir()
        mock_index = indexes_dir / "openfilter"
        mock_index.mkdir()

        with patch("code_context.indexing.INDEXES_DIR", str(indexes_dir)):
            with patch(
                "openfilter_mcp.preindex_repos.MONOREPO_CLONE_DIR", "openfilter"
            ):
                # Clear any cached import
                if "openfilter_mcp.server" in sys.modules:
                    del sys.modules["openfilter_mcp.server"]
                yield

    @pytest.mark.asyncio
    async def test_upload_video_success(self, tmp_path, mock_server_imports):
        """Should upload video file and return created video object."""
        from openfilter_mcp.server import upload_video

        # Get the underlying function from the FunctionTool wrapper
        upload_video_fn = upload_video.fn

        # Create a test file
        test_file = tmp_path / "test_video.mp4"
        create_test_video_file(test_file)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "video-123",
            "title": "Test Video",
            "gcs_uri": "gs://bucket/videos/video-123.mp4",
            "status": "processing",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.server.async_api_client", return_value=mock_client
        ):
            result = await upload_video_fn(
                project_id="project-123",
                file_path=str(test_file),
                title="Test Video",
            )

        assert result["id"] == "video-123"
        assert result["title"] == "Test Video"
        assert result["gcs_uri"] == "gs://bucket/videos/video-123.mp4"
        mock_client.post.assert_called_once()

        # Verify the call was made with correct endpoint
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/projects/project-123/videos/upload"

    @pytest.mark.asyncio
    async def test_upload_video_with_description(self, tmp_path, mock_server_imports):
        """Should include description in upload when provided."""
        from openfilter_mcp.server import upload_video

        upload_video_fn = upload_video.fn

        test_file = tmp_path / "test_video.mp4"
        create_test_video_file(test_file)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "video-456",
            "title": "Described Video",
            "description": "A test description",
            "gcs_uri": "gs://bucket/videos/video-456.mp4",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.server.async_api_client", return_value=mock_client
        ):
            result = await upload_video_fn(
                project_id="project-123",
                file_path=str(test_file),
                title="Described Video",
                description="A test description",
            )

        assert result["description"] == "A test description"

        # Verify description was included in the data
        call_args = mock_client.post.call_args
        assert call_args[1]["data"]["description"] == "A test description"

    @pytest.mark.asyncio
    async def test_upload_video_file_not_found(self, mock_server_imports):
        """Should raise FileNotFoundError for non-existent file."""
        from openfilter_mcp.server import upload_video

        upload_video_fn = upload_video.fn

        with pytest.raises(FileNotFoundError) as exc_info:
            await upload_video_fn(
                project_id="project-123",
                file_path="/nonexistent/path/video.mp4",
                title="Missing Video",
            )

        assert "Video file not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_upload_video_propagates_http_errors(
        self, tmp_path, mock_server_imports
    ):
        """Should propagate HTTP errors from the API."""
        from openfilter_mcp.server import upload_video

        upload_video_fn = upload_video.fn

        test_file = tmp_path / "test_video.mp4"
        create_test_video_file(test_file)

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
            "openfilter_mcp.server.async_api_client", return_value=mock_client
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await upload_video_fn(
                    project_id="nonexistent-project",
                    file_path=str(test_file),
                    title="Test Video",
                )

    @pytest.mark.asyncio
    async def test_upload_video_uses_filename_from_path(
        self, tmp_path, mock_server_imports
    ):
        """Should extract and use filename from the file path."""
        from openfilter_mcp.server import upload_video

        upload_video_fn = upload_video.fn

        test_file = tmp_path / "my_custom_video.mp4"
        create_test_video_file(test_file)

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "video-789"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "openfilter_mcp.server.async_api_client", return_value=mock_client
        ):
            await upload_video_fn(
                project_id="project-123",
                file_path=str(test_file),
                title="Test Video",
            )

        # Verify the filename was extracted correctly
        call_args = mock_client.post.call_args
        files_arg = call_args[1]["files"]
        assert files_arg["file"][0] == "my_custom_video.mp4"


# Functional tests that require live API
@pytest.mark.skipif(
    read_psctl_token() is None and not os.getenv("PLAINSIGHT_API_TOKEN"),
    reason="No authentication token available (run psctl login or set PLAINSIGHT_API_TOKEN)",
)
class TestUploadVideoFunctional:
    """Functional tests that make real API calls.

    These tests require:
    - Valid authentication via psctl login or PLAINSIGHT_API_TOKEN env var
    - TEST_PROJECT_ID environment variable set to a valid project
    """

    @pytest.fixture
    def project_id(self):
        """Get test project ID from environment."""
        project_id = os.getenv("TEST_PROJECT_ID")
        if not project_id:
            pytest.skip("TEST_PROJECT_ID environment variable not set")
        return project_id

    @pytest.fixture
    def test_video_file(self, tmp_path):
        """Create a minimal test video file."""
        # Create a minimal valid MP4 file (ftyp box only)
        # This is the smallest valid MP4 structure
        test_file = tmp_path / "functional_test_video.mp4"

        # Minimal MP4 with ftyp box
        ftyp_box = (
            b"\x00\x00\x00\x14"  # box size (20 bytes)
            b"ftyp"  # box type
            b"isom"  # major brand
            b"\x00\x00\x00\x00"  # minor version
            b"isom"  # compatible brand
        )

        with open(test_file, "wb") as f:
            f.write(ftyp_box)

        return test_file

    @pytest.fixture
    def mock_server_imports(self, tmp_path):
        """Mock the server module dependencies to allow import."""
        indexes_dir = tmp_path / "indexes"
        indexes_dir.mkdir()
        mock_index = indexes_dir / "openfilter"
        mock_index.mkdir()

        with patch("code_context.indexing.INDEXES_DIR", str(indexes_dir)):
            with patch(
                "openfilter_mcp.preindex_repos.MONOREPO_CLONE_DIR", "openfilter"
            ):
                # Clear any cached import
                if "openfilter_mcp.server" in sys.modules:
                    del sys.modules["openfilter_mcp.server"]
                yield

    @pytest.mark.asyncio
    async def test_upload_video_live(
        self, project_id, test_video_file, mock_server_imports
    ):
        """Functional test: upload a video to the live API."""
        from openfilter_mcp.server import upload_video

        upload_video_fn = upload_video.fn

        result = await upload_video_fn(
            project_id=project_id,
            file_path=str(test_video_file),
            title="MCP Functional Test Video",
            description="Uploaded by automated functional test",
        )

        # Verify response structure
        assert "id" in result
        assert result["id"]  # Should be non-empty

        # The API should return the video object
        print(f"Successfully uploaded video: {result.get('id')}")
        print(f"GCS URI: {result.get('gcs_uri', 'N/A')}")

    @pytest.mark.asyncio
    async def test_upload_and_verify_video_exists(
        self, project_id, test_video_file, mock_server_imports
    ):
        """Functional test: upload a video and verify it appears in the corpus."""
        from openfilter_mcp.server import get_video, upload_video

        upload_video_fn = upload_video.fn
        get_video_fn = get_video.fn

        # Upload the video
        upload_result = await upload_video_fn(
            project_id=project_id,
            file_path=str(test_video_file),
            title="MCP Verify Test Video",
            description="Testing upload and retrieval",
        )

        video_id = upload_result["id"]
        assert video_id

        # Retrieve the video to verify it was created
        video = await get_video_fn(
            project_id=project_id,
            video_id=video_id,
        )

        assert video["id"] == video_id
        assert video["title"] == "MCP Verify Test Video"
        print(f"Verified video exists: {video_id}")
