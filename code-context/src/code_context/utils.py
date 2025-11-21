"""
This module provides utility functions for the code-context library.
"""

import os
from typing import Optional

from pathspec import PathSpec

from .chunking import convert_ipynb_to_python

# Comprehensive list of source code file extensions based on GitHub Linguist
SUPPORTED_EXTENSIONS = {
    # Programming languages
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".java",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".scala",
    ".m",
    ".mm",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".pl",
    ".r",
    ".lua",
    ".dart",
    ".ex",
    ".exs",
    ".clj",
    ".cljs",
    ".erl",
    ".hrl",
    ".hs",
    ".lhs",
    ".ml",
    ".mli",
    ".elm",
    ".fs",
    ".fsi",
    ".fsx",
    ".v",
    ".sv",
    ".vhd",
    ".vhdl",
    ".sc",
    ".groovy",
    ".gradle",
    ".nim",
    ".zig",
    ".odin",
    ".cr",
    ".jl",
    ".raku",
    ".rakumod",
    # Web and markup
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".vue",
    ".svelte",
    ".astro",
    ".xml",
    ".svg",
    ".md",
    ".markdown",
    ".rst",
    ".asciidoc",
    ".adoc",
    ".org",
    ".tex",
    ".latex",
    # Data formats (useful for config)
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    # Text/documentation
    ".txt",
    ".text",
    # Scripts and config
    ".sql",
    ".graphql",
    ".proto",
    ".thrift",
}


def _load_gitignore_patterns(repo_path: str) -> PathSpec:
    """Loads .gitignore patterns from a repository path."""
    patterns = []
    for root, _, files in os.walk(repo_path):
        if ".gitignore" in files:
            gitignore_path = os.path.join(root, ".gitignore")
            with open(gitignore_path, "r", encoding="utf-8") as f:
                # Prepend the relative path from repo_path to each pattern
                relative_dir = os.path.relpath(root, repo_path)
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if relative_dir != ".":
                            patterns.append(os.path.join(relative_dir, line))
                        else:
                            patterns.append(line)
    return PathSpec.from_lines("gitwildmatch", patterns)


def walk_repo(repo_path, ignore_spec: Optional[PathSpec] = None):
    """Walks a repository and yields the content and filepath of each file, filtering by extension and .gitignore."""
    for root, _, files in os.walk(repo_path):
        for file in files:
            # Always ignore .git directories and .gitignore files themselves
            if ".git" in root or file == ".gitignore":
                continue

            filepath = os.path.join(root, file)
            relative_path = os.path.relpath(filepath, repo_path)

            # Check against .gitignore patterns
            if ignore_spec and ignore_spec.match_file(relative_path):
                continue

            file_ext = os.path.splitext(file)[1].lower()

            if file_ext == ".ipynb":
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        ipynb_content = f.read()
                    python_content = convert_ipynb_to_python(ipynb_content)
                    
                    # Create a new .py filename in the same directory as the .ipynb file
                    new_filepath = filepath.replace(".ipynb", ".py")
                    
                    with open(new_filepath, "w", encoding="utf-8") as f_py:
                        f_py.write(python_content)
                    
                    # Yield the relative path and content of the newly created .py file
                    yield os.path.relpath(new_filepath, repo_path), python_content
                    
                except Exception as e:
                    print(f"Error converting .ipynb file {filepath}: {e}")
                continue # Skip original .ipynb file

            if file_ext not in SUPPORTED_EXTENSIONS:
                continue

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    yield relative_path, f.read()
            except Exception:
                # For simplicity, we'll just ignore files that we can't read.
                pass
