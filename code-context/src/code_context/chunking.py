"""
This module provides functionality for chunking documents, including AST-based chunking for code.
"""

import os
from typing import Dict, Any, List, Optional

import nbformat
from nbconvert.exporters import ScriptExporter
from tree_sitter import Parser
from tree_sitter_language_pack import get_parser

# Node types that represent logical code units for AST-based chunking
SPLITTABLE_NODE_TYPES = {
    "javascript": [
        "function_declaration",
        "arrow_function",
        "class_declaration",
        "method_definition",
        "export_statement",
    ],
    "typescript": [
        "function_declaration",
        "arrow_function",
        "class_declaration",
        "method_definition",
        "export_statement",
        "interface_declaration",
        "type_alias_declaration",
    ],
    "python": [
        "function_definition",
        "class_definition",
        "decorated_definition",
        "async_function_definition",
    ],
    "java": [
        "method_declaration",
        "class_declaration",
        "interface_declaration",
        "constructor_declaration",
    ],
    "cpp": [
        "function_definition",
        "class_specifier",
        "namespace_definition",
        "declaration",
    ],
    "c": ["function_definition", "struct_specifier", "declaration"],
    "go": [
        "function_declaration",
        "method_declaration",
        "type_declaration",
        "var_declaration",
        "const_declaration",
    ],
    "rust": [
        "function_item",
        "impl_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "mod_item",
    ],
    "csharp": [
        "method_declaration",
        "class_declaration",
        "interface_declaration",
        "namespace_declaration",
    ],
    "scala": [
        "function_definition",
        "class_definition",
        "object_definition",
        "trait_definition",
    ],
}

# Map file extensions to tree-sitter language names
EXTENSION_TO_LANGUAGE = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".py": "python",
    ".java": "java",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".go": "go",
    ".rs": "rust",
    ".scala": "scala",
    ".sc": "scala",
}


def convert_ipynb_to_python(ipynb_content: str) -> str:
    """Converts the content of an .ipynb file to a Python string."""
    notebook = nbformat.reads(ipynb_content, as_version=4)
    exporter = ScriptExporter()
    python_code, _ = exporter.from_notebook_node(notebook)
    return python_code


def get_language_from_filepath(filepath: str) -> Optional[str]:
    """Get the tree-sitter language name from a file path."""
    ext = os.path.splitext(filepath)[1].lower()
    return EXTENSION_TO_LANGUAGE.get(ext)


def normalize_language_name(lang: str) -> str:
    """Normalize tree-sitter language name to match SPLITTABLE_NODE_TYPES keys."""
    lang_map = {
        "c_sharp": "csharp",
        "tsx": "typescript",
    }
    return lang_map.get(lang, lang)


def chunk_document_ast(
    document: str, filepath: str, chunk_size: int = 2500, chunk_overlap: int = 300
) -> List[Dict[str, Any]]:
    """
    Chunks a document using tree-sitter AST analysis for semantic boundaries.
    Falls back to character-based chunking if AST parsing fails or language is unsupported.
    """
    # Try to get the language from the file extension
    ts_language = get_language_from_filepath(filepath)

    if ts_language is None:
        # Language not supported by tree-sitter, fall back to character-based chunking
        return chunk_document(document, chunk_size, chunk_overlap)

    try:
        # Get the parser for this language
        parser = get_parser(ts_language)
        tree = parser.parse(bytes(document, "utf-8"))

        if not tree.root_node:
            # Failed to parse, fall back to character-based chunking
            return chunk_document(document, chunk_size, chunk_overlap)

        # Get the splittable node types for this language
        normalized_lang = normalize_language_name(ts_language)
        splittable_types = SPLITTABLE_NODE_TYPES.get(normalized_lang, [])

        if not splittable_types:
            # No node types defined for this language, fall back
            return chunk_document(document, chunk_size, chunk_overlap)

        # Extract chunks based on AST nodes
        chunks = extract_chunks_from_ast(tree.root_node, document, splittable_types)

        # If no chunks found, fall back to character-based
        if not chunks:
            return chunk_document(document, chunk_size, chunk_overlap)

        # Refine chunks that are too large
        refined_chunks = refine_large_chunks(
            chunks, document, chunk_size, chunk_overlap
        )

        return refined_chunks

    except Exception as e:
        # If anything goes wrong, fall back to character-based chunking
        print(
            f"AST chunking failed for {filepath}: {e}, falling back to character-based"
        )
        return chunk_document(document, chunk_size, chunk_overlap)


def extract_chunks_from_ast(
    node, code: str, splittable_types: List[str]
) -> List[Dict[str, Any]]:
    """
    Traverse the AST and extract chunks based on splittable node types.
    """
    chunks = []
    code_bytes = bytes(code, "utf-8")

    def traverse(current_node):
        # Check if this node type should be split into a chunk
        if current_node.type in splittable_types:
            start_line = current_node.start_point[0] + 1  # 1-indexed
            end_line = current_node.end_point[0] + 1
            start_byte = current_node.start_byte
            end_byte = current_node.end_byte

            # Extract the node text
            node_text = code_bytes[start_byte:end_byte].decode("utf-8")

            # Only create chunk if it has meaningful content
            if node_text.strip():
                chunks.append(
                    {
                        "content": node_text,
                        "startLine": start_line,
                        "endLine": end_line,
                        "startByte": start_byte,
                        "endByte": end_byte,
                    }
                )

        # Continue traversing child nodes
        for child in current_node.children:
            traverse(child)

    traverse(node)

    # If no meaningful chunks found, create a single chunk with the entire code
    if not chunks:
        lines = code.split("\n")
        chunks.append(
            {
                "content": code,
                "startLine": 1,
                "endLine": len(lines),
                "startByte": 0,
                "endByte": len(code_bytes),
            }
        )

    return chunks


def refine_large_chunks(
    chunks: List[Dict[str, Any]],
    original_code: str,
    chunk_size: int,
    chunk_overlap: int,
) -> List[Dict[str, Any]]:
    """
    Split chunks that exceed the chunk_size limit using character-based chunking.
    """
    refined_chunks = []

    for chunk in chunks:
        if len(chunk["content"]) <= chunk_size:
            refined_chunks.append(chunk)
        else:
            # Chunk is too large, split it using character-based approach
            sub_chunks = chunk_document(chunk["content"], chunk_size, chunk_overlap)

            # Adjust line numbers for sub-chunks
            base_start_line = chunk["startLine"]
            base_start_byte = chunk["startByte"]

            for sub_chunk in sub_chunks:
                # Adjust the line and byte offsets
                adjusted_chunk = {
                    "content": sub_chunk["content"],
                    "startLine": base_start_line + sub_chunk["startLine"] - 1,
                    "endLine": base_start_line + sub_chunk["endLine"] - 1,
                    "startByte": base_start_byte + sub_chunk["startByte"],
                    "endByte": base_start_byte + sub_chunk["endByte"],
                }
                refined_chunks.append(adjusted_chunk)

    return refined_chunks


def chunk_document(document, chunk_size=1000, chunk_overlap=200):
    """Chunks a document using a character-based approach with overlap, tracking byte offsets."""
    lines = document.split("\n")
    chunks = []
    current_chunk = []
    current_size = 0
    start_line = 1
    start_byte = 0
    current_byte = 0

    for i, line in enumerate(lines):
        line_size = len(line) + 1  # +1 for newline

        if current_size + line_size > chunk_size and current_chunk:
            chunk_content = "\n".join(current_chunk)
            end_byte = current_byte
            chunks.append(
                {
                    "content": chunk_content,
                    "startLine": start_line,
                    "endLine": start_line + len(current_chunk) - 1,
                    "startByte": start_byte,
                    "endByte": end_byte,
                }
            )

            if chunk_content:
                overlap_lines = min(
                    int(chunk_overlap / (len(chunk_content) / len(current_chunk))),
                    len(current_chunk),
                )
            else:
                overlap_lines = 0

            new_start_line = start_line + len(current_chunk) - overlap_lines
            # Calculate byte offset for overlap start
            overlap_content = "\n".join(current_chunk[-overlap_lines:])
            start_byte = end_byte - len(overlap_content.encode("utf-8"))
            current_chunk = current_chunk[-overlap_lines:]
            current_size = len("\n".join(current_chunk))
            start_line = new_start_line

        current_chunk.append(line)
        current_size += line_size
        current_byte += line_size

    if current_chunk:
        chunk_content = "\n".join(current_chunk)
        chunks.append(
            {
                "content": chunk_content,
                "startLine": start_line,
                "endLine": start_line + len(current_chunk) - 1,
                "startByte": start_byte,
                "endByte": current_byte,
            }
        )

    return [chunk for chunk in chunks if chunk["content"].strip()]
