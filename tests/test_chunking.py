"""
Unit tests for the chunking module.
"""

import pytest
from code_context.chunking import (
    chunk_document,
    chunk_document_ast,
    convert_ipynb_to_python,
    get_language_from_filepath,
    normalize_language_name,
    refine_large_chunks,
)


class TestChunkDocument:
    """Tests for the chunk_document function."""

    def test_basic_chunking(self):
        """Test basic document chunking with small chunk size."""
        document = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
        chunks = chunk_document(document, chunk_size=20, chunk_overlap=5)

        assert len(chunks) > 0
        assert all("content" in chunk for chunk in chunks)
        assert all("startLine" in chunk for chunk in chunks)
        assert all("endLine" in chunk for chunk in chunks)

    def test_chunk_overlap(self):
        """Test that chunks have proper overlap."""
        document = "\n".join([f"Line {i}" for i in range(10)])
        chunks = chunk_document(document, chunk_size=30, chunk_overlap=10)

        assert len(chunks) >= 2
        # Verify that consecutive chunks have some overlapping content
        if len(chunks) >= 2:
            # Check that line ranges overlap
            assert chunks[0]["endLine"] >= chunks[1]["startLine"]

    def test_empty_document(self):
        """Test chunking an empty document."""
        chunks = chunk_document("", chunk_size=100, chunk_overlap=10)
        assert len(chunks) == 0

    def test_single_line_document(self):
        """Test chunking a single line document."""
        document = "This is a single line"
        chunks = chunk_document(document, chunk_size=100, chunk_overlap=10)

        assert len(chunks) == 1
        assert chunks[0]["content"] == document
        assert chunks[0]["startLine"] == 1
        assert chunks[0]["endLine"] == 1

    def test_byte_offsets(self):
        """Test that byte offsets are correctly calculated."""
        document = "Hello\nWorld\nTest"
        chunks = chunk_document(document, chunk_size=100, chunk_overlap=0)

        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk["startByte"] == 0
        # Account for newlines in byte count
        assert chunk["endByte"] > 0


class TestConvertIpynbToPython:
    """Tests for the convert_ipynb_to_python function."""

    def test_basic_notebook_conversion(self):
        """Test converting a basic Jupyter notebook to Python."""
        notebook_json = """{
            "cells": [
                {
                    "cell_type": "code",
                    "source": ["print('hello')"],
                    "metadata": {},
                    "outputs": [],
                    "execution_count": null
                }
            ],
            "metadata": {
                "kernelspec": {
                    "display_name": "Python 3",
                    "language": "python",
                    "name": "python3"
                }
            },
            "nbformat": 4,
            "nbformat_minor": 2
        }"""

        result = convert_ipynb_to_python(notebook_json)
        assert "print('hello')" in result

    def test_notebook_with_markdown(self):
        """Test that markdown cells are handled properly."""
        notebook_json = """{
            "cells": [
                {
                    "cell_type": "markdown",
                    "source": ["# Header"],
                    "metadata": {}
                },
                {
                    "cell_type": "code",
                    "source": ["x = 1"],
                    "metadata": {},
                    "outputs": [],
                    "execution_count": null
                }
            ],
            "metadata": {
                "kernelspec": {
                    "display_name": "Python 3",
                    "language": "python",
                    "name": "python3"
                }
            },
            "nbformat": 4,
            "nbformat_minor": 2
        }"""

        result = convert_ipynb_to_python(notebook_json)
        assert "x = 1" in result


class TestGetLanguageFromFilepath:
    """Tests for the get_language_from_filepath function."""

    def test_python_file(self):
        """Test identifying Python files."""
        assert get_language_from_filepath("test.py") == "python"

    def test_javascript_file(self):
        """Test identifying JavaScript files."""
        assert get_language_from_filepath("test.js") == "javascript"

    def test_typescript_file(self):
        """Test identifying TypeScript files."""
        assert get_language_from_filepath("test.ts") == "typescript"

    def test_unsupported_extension(self):
        """Test handling unsupported file extensions."""
        assert get_language_from_filepath("test.xyz") is None

    def test_case_insensitive(self):
        """Test that file extension detection is case-insensitive."""
        assert get_language_from_filepath("test.PY") == "python"


class TestNormalizeLanguageName:
    """Tests for the normalize_language_name function."""

    def test_c_sharp_normalization(self):
        """Test normalizing c_sharp to csharp."""
        assert normalize_language_name("c_sharp") == "csharp"

    def test_tsx_normalization(self):
        """Test normalizing tsx to typescript."""
        assert normalize_language_name("tsx") == "typescript"

    def test_no_normalization_needed(self):
        """Test that languages without aliases remain unchanged."""
        assert normalize_language_name("python") == "python"
        assert normalize_language_name("javascript") == "javascript"


class TestChunkDocumentAst:
    """Tests for the chunk_document_ast function."""

    def test_python_function_chunking(self):
        """Test chunking a Python file with functions."""
        python_code = """
def function_one():
    return 1

def function_two():
    return 2

class MyClass:
    def method(self):
        pass
"""
        chunks = chunk_document_ast(python_code, "test.py", chunk_size=500, chunk_overlap=50)

        assert len(chunks) > 0
        # Should create chunks for each function/class
        assert any("function_one" in chunk["content"] for chunk in chunks)

    def test_unsupported_language_fallback(self):
        """Test that unsupported languages fall back to character-based chunking."""
        content = "Some random text\nMore text\nEven more text"
        chunks = chunk_document_ast(content, "test.xyz", chunk_size=20, chunk_overlap=5)

        # Should fall back to character-based chunking
        assert len(chunks) > 0
        assert all("content" in chunk for chunk in chunks)

    def test_empty_file(self):
        """Test chunking an empty file."""
        chunks = chunk_document_ast("", "test.py", chunk_size=100, chunk_overlap=10)
        # Empty strings still create a single chunk, but it gets filtered out by the final filter
        # However, AST parsing of empty file creates one chunk with empty content
        assert len(chunks) <= 1
        if len(chunks) == 1:
            assert chunks[0]["content"] == ""

    def test_invalid_syntax_fallback(self):
        """Test that invalid syntax falls back to character-based chunking."""
        invalid_python = "def function_one(\nthis is not valid python"
        chunks = chunk_document_ast(invalid_python, "test.py", chunk_size=100, chunk_overlap=10)

        # Should fall back to character-based chunking
        assert len(chunks) > 0


class TestRefineLargeChunks:
    """Tests for the refine_large_chunks function."""

    def test_small_chunks_unchanged(self):
        """Test that chunks smaller than chunk_size are not split."""
        chunks = [
            {"content": "small chunk", "startLine": 1, "endLine": 1, "startByte": 0, "endByte": 11}
        ]
        refined = refine_large_chunks(chunks, "small chunk", chunk_size=100, chunk_overlap=10)

        assert len(refined) == 1
        assert refined[0]["content"] == "small chunk"

    def test_large_chunk_splitting(self):
        """Test that large chunks are split into smaller ones."""
        large_content = "\n".join([f"Line {i}" for i in range(100)])
        chunks = [
            {"content": large_content, "startLine": 1, "endLine": 100, "startByte": 0, "endByte": len(large_content)}
        ]
        refined = refine_large_chunks(chunks, large_content, chunk_size=100, chunk_overlap=10)

        # Should be split into multiple chunks
        assert len(refined) > 1

    def test_mixed_chunk_sizes(self):
        """Test refining a mix of small and large chunks."""
        small_chunk = {"content": "small", "startLine": 1, "endLine": 1, "startByte": 0, "endByte": 5}
        large_content = "\n".join([f"Line {i}" for i in range(50)])
        large_chunk = {"content": large_content, "startLine": 2, "endLine": 51, "startByte": 6, "endByte": len(large_content) + 6}

        chunks = [small_chunk, large_chunk]
        refined = refine_large_chunks(chunks, "small\n" + large_content, chunk_size=50, chunk_overlap=10)

        # First chunk should remain unchanged, second should be split
        assert len(refined) > 2
        assert refined[0]["content"] == "small"
