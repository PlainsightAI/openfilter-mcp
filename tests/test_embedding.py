"""
Unit tests for the embedding module.
"""

import numpy as np
import pytest
from unittest.mock import Mock, patch
from code_context.embedding import (
    get_embedding,
    get_embeddings,
    INSTRUCTION_CONFIG,
)


class TestInstructionConfig:
    """Tests for the INSTRUCTION_CONFIG constant."""

    def test_all_modes_have_query_and_passage(self):
        """Test that all instruction modes have both query and passage prefixes."""
        for mode, config in INSTRUCTION_CONFIG.items():
            assert "query" in config, f"Mode {mode} missing 'query' key"
            assert "passage" in config, f"Mode {mode} missing 'passage' key"
            assert isinstance(config["query"], str)
            assert isinstance(config["passage"], str)

    def test_common_modes_exist(self):
        """Test that common instruction modes are defined."""
        expected_modes = ["nl2code", "qa", "code2code", "code2nl", "code2completion"]
        for mode in expected_modes:
            assert mode in INSTRUCTION_CONFIG


class TestGetEmbedding:
    """Tests for the get_embedding function."""

    @patch("code_context.embedding.get_model")
    def test_basic_embedding_generation(self, mock_get_model):
        """Test basic embedding generation."""
        # Mock the model
        mock_model = Mock()
        mock_embedding = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
        mock_model.create_embedding.return_value = {
            "data": [{"embedding": mock_embedding.tolist()}]
        }
        mock_get_model.return_value = mock_model

        # Call the function
        result = get_embedding("test code")

        # Verify the model was called
        mock_model.create_embedding.assert_called_once_with("test code")

        # Verify the result is normalized
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32
        # Check that it's normalized (L2 norm should be 1)
        np.testing.assert_almost_equal(np.linalg.norm(result), 1.0, decimal=5)

    @patch("code_context.embedding.get_model")
    def test_embedding_normalization(self, mock_get_model):
        """Test that embeddings are properly normalized."""
        # Create a non-normalized vector
        mock_model = Mock()
        unnormalized = np.array([3.0, 4.0], dtype=np.float32)  # Norm = 5.0
        mock_model.create_embedding.return_value = {
            "data": [{"embedding": unnormalized.tolist()}]
        }
        mock_get_model.return_value = mock_model

        result = get_embedding("test")

        # Check normalization
        expected = unnormalized / 5.0
        np.testing.assert_array_almost_equal(result, expected, decimal=5)

    @patch("code_context.embedding.get_model")
    def test_multidimensional_embedding_takes_last(self, mock_get_model):
        """Test that multi-dimensional embeddings use the last token embedding."""
        # Mock a 2D embedding (multiple tokens)
        mock_model = Mock()
        multi_dim_embedding = np.array([
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
            [0.7, 0.8, 0.9],
        ], dtype=np.float32)
        mock_model.create_embedding.return_value = {
            "data": [{"embedding": multi_dim_embedding.tolist()}]
        }
        mock_get_model.return_value = mock_model

        result = get_embedding("test code")

        # Should take the last embedding and normalize it
        last_embedding = multi_dim_embedding[-1]
        expected = last_embedding / np.linalg.norm(last_embedding)
        np.testing.assert_array_almost_equal(result, expected, decimal=5)

    @patch("code_context.embedding.get_model")
    def test_zero_vector_handling(self, mock_get_model):
        """Test handling of zero vectors (edge case)."""
        mock_model = Mock()
        zero_vector = np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float32)
        mock_model.create_embedding.return_value = {
            "data": [{"embedding": zero_vector.tolist()}]
        }
        mock_get_model.return_value = mock_model

        result = get_embedding("empty text")

        # Should return zero vector without division by zero error
        assert isinstance(result, np.ndarray)
        np.testing.assert_array_equal(result, zero_vector)


class TestGetEmbeddings:
    """Tests for the get_embeddings function (batch processing)."""

    @patch("code_context.embedding.get_embedding")
    def test_batch_embedding_generation(self, mock_get_embedding):
        """Test generating embeddings for multiple texts."""
        # Mock embeddings for each text
        mock_embeddings = [
            np.array([0.1, 0.2, 0.3], dtype=np.float32),
            np.array([0.4, 0.5, 0.6], dtype=np.float32),
            np.array([0.7, 0.8, 0.9], dtype=np.float32),
        ]
        mock_get_embedding.side_effect = mock_embeddings

        texts = ["text1", "text2", "text3"]
        results = get_embeddings(texts)

        # Verify all texts were processed
        assert len(results) == 3
        assert mock_get_embedding.call_count == 3
        for i, text in enumerate(texts):
            np.testing.assert_array_equal(results[i], mock_embeddings[i])

    @patch("code_context.embedding.get_embedding")
    def test_empty_text_list(self, mock_get_embedding):
        """Test handling of empty text list."""
        results = get_embeddings([])

        assert len(results) == 0
        mock_get_embedding.assert_not_called()

    @patch("code_context.embedding.get_embedding")
    def test_progress_callback(self, mock_get_embedding):
        """Test that progress callback is called correctly."""
        mock_get_embedding.return_value = np.array([0.1, 0.2, 0.3], dtype=np.float32)

        progress_calls = []

        def progress_callback(current, total):
            progress_calls.append((current, total))

        texts = ["text1", "text2", "text3"]
        get_embeddings(texts, progress_callback=progress_callback)

        # Verify progress callback was called for each text
        assert len(progress_calls) == 3
        assert progress_calls[0] == (1, 3)
        assert progress_calls[1] == (2, 3)
        assert progress_calls[2] == (3, 3)

    @patch("code_context.embedding.get_embedding")
    def test_single_text(self, mock_get_embedding):
        """Test processing a single text."""
        mock_embedding = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        mock_get_embedding.return_value = mock_embedding

        results = get_embeddings(["single text"])

        assert len(results) == 1
        np.testing.assert_array_equal(results[0], mock_embedding)
