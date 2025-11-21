"""
This module provides the core indexing functionality for the code-context library.
"""

import faiss
import git
import hashlib
import json
import numpy as np
import os
import shutil
import traceback
from datetime import datetime

from .chunking import chunk_document_ast
from .embedding import get_embeddings, INSTRUCTION_CONFIG
from .utils import walk_repo, _load_gitignore_patterns

INDEXES_DIR = "indexes"
CLONES_DIR = "clones"


def _do_index(job_id: str, repo_url: str, force: bool, is_local: bool = False, _indexing_jobs: dict = None, _indexing_lock=None):
    """Internal function that performs the actual indexing work."""
    try:
        # Update job status to processing before calling the direct indexing function
        with _indexing_lock:
            _indexing_jobs[job_id]["status"] = "processing"
            _indexing_jobs[job_id]["message"] = f"Starting indexing for {repo_url}"

        index_name = index_repository_direct(repo_url, force, is_local, job_id, _indexing_jobs, _indexing_lock)

        with _indexing_lock:
            _indexing_jobs[job_id]["status"] = "completed"
            _indexing_jobs[job_id]["message"] = (
                f"Successfully indexed {repo_url} into {index_name}"
            )
            _indexing_jobs[job_id]["completed_at"] = datetime.now().isoformat()
    except Exception as e:
        tb = traceback.format_exc()
        with _indexing_lock:
            _indexing_jobs[job_id]["status"] = "failed"
            _indexing_jobs[job_id]["message"] = f"Error during indexing: {str(e)}"
            _indexing_jobs[job_id]["error"] = str(e)
            _indexing_jobs[job_id]["traceback"] = tb
            _indexing_jobs[job_id]["completed_at"] = datetime.now().isoformat()

def index_repository_direct(repo_url: str, force: bool = False, is_local: bool = False, job_id: str = None, _indexing_jobs: dict = None, _indexing_lock=None) -> str:
    if is_local:
        # For local paths, use basename + hash of path
        abs_path = os.path.abspath(repo_url)
        if not os.path.exists(abs_path):
            raise ValueError(f"Local path does not exist: {abs_path}")

        path_hash = hashlib.sha256(abs_path.encode()).hexdigest()[:8]
        repo_name = os.path.basename(abs_path.rstrip("/"))
        index_name = f"{repo_name}-{path_hash}"
        index_path = os.path.join(INDEXES_DIR, index_name)
        clone_path = abs_path  # Use the local path directly
        revision = path_hash

        # Store original_repo_path and is_local flag for local indexes
        os.makedirs(
            index_path, exist_ok=True
        )  # Ensure index_path exists before writing config

        if _indexing_lock and _indexing_jobs and job_id:
            with _indexing_lock:
                _indexing_jobs[job_id]["status"] = "processing"
                _indexing_jobs[job_id]["message"] = (
                    f"Processing local directory: {abs_path}"
                )
                _indexing_jobs[job_id]["index_name"] = index_name
                _indexing_jobs[job_id]["revision"] = revision
        else:
            print(f"Processing local directory: {abs_path}")

        ignore_spec = _load_gitignore_patterns(clone_path)
    else:
        if _indexing_lock and _indexing_jobs and job_id:
            with _indexing_lock:
                _indexing_jobs[job_id]["status"] = "cloning"
                _indexing_jobs[job_id]["message"] = "Fetching repository information..."
        else:
            print("Fetching repository information...")

        repo_name = os.path.basename(repo_url).replace(".git", "")

        # Get the latest revision hash without cloning the whole repo
        try:
            from git.cmd import Git

            g = Git()
            blob = g.ls_remote(repo_url, heads=True)
            revision = blob.split()[0]
        except Exception:
            # Fallback if ls_remote fails
            revision = "latest"

        clone_path = os.path.join(CLONES_DIR, f"{repo_name}-{revision}")
        index_name = f"{repo_name}-{revision}"
        index_path = os.path.join(INDEXES_DIR, index_name)

        # Store original_repo_path and is_local flag for remote indexes
        os.makedirs(
            index_path, exist_ok=True
        )  # Ensure index_path exists before writing config
        if _indexing_lock and _indexing_jobs and job_id:
            with _indexing_lock:
                _indexing_jobs[job_id]["index_name"] = index_name
                _indexing_jobs[job_id]["revision"] = revision

    # Clean up old clones and indexes if force=True
    if force:
        if _indexing_lock and _indexing_jobs and job_id:
            with _indexing_lock:
                _indexing_jobs[job_id]["message"] = (
                    "Cleaning up old clones and indexes..."
                )
        else:
            print("Cleaning up old clones and indexes...")

        # Remove old clone if it exists (only for git repos, not local paths)
        if not is_local and os.path.exists(clone_path):
            shutil.rmtree(clone_path)

        # Remove old index if it exists
        if os.path.exists(index_path):
            shutil.rmtree(index_path)

    # Check if already indexed (skip for local paths or when force=True)
    if not is_local and os.path.exists(clone_path) and not force:
        if _indexing_lock and _indexing_jobs and job_id:
            with _indexing_lock:
                _indexing_jobs[job_id]["status"] = "completed"
                _indexing_jobs[job_id]["message"] = (
                    f"Repository {repo_url} at revision {revision} is already cloned and likely indexed."
                )
                _indexing_jobs[job_id]["completed_at"] = datetime.now().isoformat()
        else:
            print(f"Repository {repo_url} at revision {revision} is already cloned and likely indexed.")
        return index_name

    # Clone the repository (only if it's a remote git repo)
    if not is_local:
        if _indexing_lock and _indexing_jobs and job_id:
            with _indexing_lock:
                _indexing_jobs[job_id]["message"] = (
                    f"Cloning repository from {repo_url}..."
                )
        else:
            print(f"Cloning repository from {repo_url}...")

        git.Repo.clone_from(repo_url, clone_path)
        ignore_spec = _load_gitignore_patterns(clone_path)

    if _indexing_lock and _indexing_jobs and job_id:
        with _indexing_lock:
            _indexing_jobs[job_id]["status"] = "chunking"
            _indexing_jobs[job_id]["message"] = "Processing and chunking documents..."
    else:
        print("Processing and chunking documents...")

    os.makedirs(index_path, exist_ok=True)

    documents = []
    metadata = []

    for filepath, content in walk_repo(clone_path, ignore_spec):
        chunks = chunk_document_ast(content, filepath)
        for chunk in chunks:
            # Use nl2code passage prefix for code chunks
            prefixed_content = (
                INSTRUCTION_CONFIG["nl2code"]["passage"] + chunk["content"]
            )
            documents.append(prefixed_content)
            metadata.append(
                {
                    "filepath": filepath,
                    "startLine": chunk["startLine"],
                    "endLine": chunk["endLine"],
                    "startByte": chunk["startByte"],
                    "endByte": chunk["endByte"],
                }
            )

    if _indexing_lock and _indexing_jobs and job_id:
        with _indexing_lock:
            _indexing_jobs[job_id]["total_chunks"] = len(documents)
            _indexing_jobs[job_id]["chunks_indexed"] = 0
            _indexing_jobs[job_id]["status"] = "embedding"
            _indexing_jobs[job_id]["message"] = (
                f"Generating embeddings for {len(documents)} chunks..."
            )
    else:
        print(f"Generating embeddings for {len(documents)} chunks...")

    def update_progress(current, total):
        if _indexing_lock and _indexing_jobs and job_id:
            with _indexing_lock:
                _indexing_jobs[job_id]["chunks_indexed"] = current
                _indexing_jobs[job_id]["message"] = (
                    f"Generating embeddings: {current}/{total} chunks..."
                )
        else:
            print(f"Generating embeddings: {current}/{total} chunks...")

    document_embeddings = get_embeddings(
        documents, progress_callback=update_progress
    )

    if _indexing_lock and _indexing_jobs and job_id:
        with _indexing_lock:
            _indexing_jobs[job_id]["status"] = "indexing"
            _indexing_jobs[job_id]["message"] = "Creating FAISS index..."
    else:
        print("Creating FAISS index...")

    # Stack embeddings into a matrix for FAISS
    embeddings_matrix = np.stack(document_embeddings).astype("float32")

    # Create FAISS flat index with inner product (for normalized vectors, this is cosine similarity)
    dimension = embeddings_matrix.shape[1]
    index = faiss.IndexFlatIP(dimension)

    # Add vectors to the index
    index.add(embeddings_matrix)

    # Save FAISS index

    faiss_index_path = os.path.join(index_path, "faiss.index")
    faiss.write_index(index, faiss_index_path)

    # Save chunk mapping file
    metadata_path = os.path.join(index_path, "chunks_metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    index_config_path = os.path.join(index_path, "index_config.json")
    with open(index_config_path, "w", encoding="utf-8") as f:
        json.dump(
            {"original_repo_path": clone_path, "is_local": is_local}, f, indent=2
        )
    return index_name
