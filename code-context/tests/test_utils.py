"""
Unit tests for the utils module.
"""

import os
import tempfile
from pathspec import PathSpec
from code_context.utils import (
    SUPPORTED_EXTENSIONS,
    _load_gitignore_patterns,
    walk_repo,
)


class TestSupportedExtensions:
    """Tests for SUPPORTED_EXTENSIONS constant."""

    def test_common_languages_included(self):
        """Test that common programming language extensions are supported."""
        common_extensions = {".py", ".js", ".ts", ".java", ".cpp", ".c", ".go", ".rs"}
        assert common_extensions.issubset(SUPPORTED_EXTENSIONS)

    def test_web_languages_included(self):
        """Test that web-related extensions are supported."""
        web_extensions = {".html", ".css", ".jsx", ".tsx", ".vue"}
        assert web_extensions.issubset(SUPPORTED_EXTENSIONS)

    def test_config_formats_included(self):
        """Test that configuration file formats are supported."""
        config_extensions = {".json", ".yaml", ".yml", ".toml"}
        assert config_extensions.issubset(SUPPORTED_EXTENSIONS)


class TestLoadGitignorePatterns:
    """Tests for the _load_gitignore_patterns function."""

    def test_empty_directory(self):
        """Test loading gitignore patterns from a directory without .gitignore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = _load_gitignore_patterns(tmpdir)
            assert isinstance(spec, PathSpec)
            # Should be empty or have no patterns
            assert not spec.match_file("test.py")

    def test_single_gitignore(self):
        """Test loading patterns from a single .gitignore file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gitignore_path = os.path.join(tmpdir, ".gitignore")
            with open(gitignore_path, "w") as f:
                f.write("*.pyc\n")
                f.write("__pycache__/\n")

            spec = _load_gitignore_patterns(tmpdir)
            assert spec.match_file("test.pyc")
            assert spec.match_file("__pycache__/module.py")
            assert not spec.match_file("test.py")

    def test_nested_gitignore(self):
        """Test loading patterns from nested .gitignore files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Root .gitignore
            with open(os.path.join(tmpdir, ".gitignore"), "w") as f:
                f.write("*.log\n")

            # Nested directory with its own .gitignore
            nested_dir = os.path.join(tmpdir, "subdir")
            os.makedirs(nested_dir)
            with open(os.path.join(nested_dir, ".gitignore"), "w") as f:
                f.write("*.tmp\n")

            spec = _load_gitignore_patterns(tmpdir)
            assert spec.match_file("test.log")
            assert spec.match_file("subdir/test.tmp")

    def test_comment_lines_ignored(self):
        """Test that comment lines in .gitignore are ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gitignore_path = os.path.join(tmpdir, ".gitignore")
            with open(gitignore_path, "w") as f:
                f.write("# This is a comment\n")
                f.write("*.pyc\n")
                f.write("# Another comment\n")

            spec = _load_gitignore_patterns(tmpdir)
            assert spec.match_file("test.pyc")


class TestWalkRepo:
    """Tests for the walk_repo function."""

    def test_basic_file_walking(self):
        """Test basic repository walking without .gitignore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some test files
            test_files = {
                "test1.py": "print('test1')",
                "test2.js": "console.log('test2')",
                "readme.md": "# README",
            }

            for filename, content in test_files.items():
                with open(os.path.join(tmpdir, filename), "w") as f:
                    f.write(content)

            results = list(walk_repo(tmpdir))
            filenames = [os.path.basename(path) for path, _ in results]

            assert "test1.py" in filenames
            assert "test2.js" in filenames
            assert "readme.md" in filenames

    def test_unsupported_extensions_filtered(self):
        """Test that files with unsupported extensions are filtered out."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create files with various extensions
            with open(os.path.join(tmpdir, "test.py"), "w") as f:
                f.write("print('hello')")
            with open(os.path.join(tmpdir, "test.xyz"), "w") as f:
                f.write("unsupported")
            with open(os.path.join(tmpdir, "binary.exe"), "w") as f:
                f.write("binary data")

            results = list(walk_repo(tmpdir))
            filenames = [os.path.basename(path) for path, _ in results]

            assert "test.py" in filenames
            assert "test.xyz" not in filenames
            assert "binary.exe" not in filenames

    def test_gitignore_filtering(self):
        """Test that .gitignore patterns are respected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create .gitignore
            with open(os.path.join(tmpdir, ".gitignore"), "w") as f:
                f.write("*.pyc\n")
                f.write("ignored_dir/\n")

            # Create test files
            with open(os.path.join(tmpdir, "test.py"), "w") as f:
                f.write("print('hello')")
            with open(os.path.join(tmpdir, "test.pyc"), "w") as f:
                f.write("bytecode")

            # Create ignored directory
            ignored_dir = os.path.join(tmpdir, "ignored_dir")
            os.makedirs(ignored_dir)
            with open(os.path.join(ignored_dir, "ignored.py"), "w") as f:
                f.write("ignored code")

            spec = _load_gitignore_patterns(tmpdir)
            results = list(walk_repo(tmpdir, ignore_spec=spec))
            filenames = [os.path.basename(path) for path, _ in results]

            assert "test.py" in filenames
            assert "test.pyc" not in filenames
            assert "ignored.py" not in filenames

    def test_git_directory_excluded(self):
        """Test that .git directories are always excluded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create .git directory
            git_dir = os.path.join(tmpdir, ".git")
            os.makedirs(git_dir)
            with open(os.path.join(git_dir, "config"), "w") as f:
                f.write("git config")

            # Create normal file
            with open(os.path.join(tmpdir, "test.py"), "w") as f:
                f.write("print('hello')")

            results = list(walk_repo(tmpdir))
            paths = [path for path, _ in results]

            # .git directory should be excluded
            assert not any(".git" in path for path in paths)
            assert any("test.py" in path for path in paths)

    def test_jupyter_notebook_conversion(self):
        """Test that .ipynb files are converted to .py and yielded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            notebook_path = os.path.join(tmpdir, "test.ipynb")
            notebook_json = """{
                "cells": [
                    {
                        "cell_type": "code",
                        "source": ["x = 42"],
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

            with open(notebook_path, "w") as f:
                f.write(notebook_json)

            results = list(walk_repo(tmpdir))

            # Should yield the converted .py file
            assert len(results) == 1
            filepath, content = results[0]
            assert filepath.endswith(".py")
            assert "x = 42" in content

    def test_nested_directory_structure(self):
        """Test walking a nested directory structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create nested structure
            os.makedirs(os.path.join(tmpdir, "src", "utils"))

            with open(os.path.join(tmpdir, "src", "main.py"), "w") as f:
                f.write("main code")
            with open(os.path.join(tmpdir, "src", "utils", "helper.py"), "w") as f:
                f.write("helper code")

            results = list(walk_repo(tmpdir))
            paths = [path for path, _ in results]

            assert len(paths) == 2
            assert any("main.py" in path for path in paths)
            assert any("helper.py" in path for path in paths)
