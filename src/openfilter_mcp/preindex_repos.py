import sys

import httpx
import jq # Assuming python-jq library
import os
import time
import git
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from code_context.indexing import index_repository_direct
except ImportError:
    index_repository_direct = None

MONOREPO_CLONE_DIR = "openfilter_repos_clones"

def preindex_openfilter_repos(org_name="plainsightai", name_filter=""):
    """
    Fetches repositories from a GitHub organization, filters them by name,
    clones them into a monorepo directory, and then indexes the monorepo
    using code_context's core indexing function.
    """
    if index_repository_direct is None:
        print("Error: code-context is not installed. Install with: uv sync --group code-search")
        sys.exit(1)

    print(f"Fetching repositories for organization: {org_name}")
    headers = {"Accept": "application/vnd.github.v3+json"}
    github_token = os.getenv("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    try:
        results = httpx.get(
            f"https://api.github.com/orgs/{org_name}/repos",
            params={"per_page": 100, "type": "public"},
            headers=headers,
            timeout=30.0
        )
        results.raise_for_status()
    except httpx.RequestError as e:
        print(f"An error occurred while requesting {e.request.url!r}: {e}")
        return
    except httpx.HTTPStatusError as e:
        print(f"Error response {e.response.status_code} while requesting {e.request.url!r}: {e}")
        return

    print("Successfully fetched repository list.")

    if name_filter:
        query_str = f'.[] | select(.name | contains("{name_filter}")) | .clone_url'
    else:
        query_str = '.[] | .clone_url'

    try:
        query = jq.compile(query_str)
        repos = query.input_text(results.text).all()
    except Exception as e:
        print(f"Error processing JSON with jq: {e}")
        print(f"Query string used: {query_str}")
        return

    if not repos:
        print(f"No repositories found matching the filter '{name_filter}'.")
        return

    print(f"Found {len(repos)} repositories to process:")

    # Create and clean the monorepo directory
    if os.path.exists(MONOREPO_CLONE_DIR):
        shutil.rmtree(MONOREPO_CLONE_DIR)
    os.makedirs(MONOREPO_CLONE_DIR)
    print(f"Created monorepo directory: {MONOREPO_CLONE_DIR}")

    cloned_repos_count = 0
    
    def _clone_one(repo_url):
        """Clone a single repository and return True on success, False on error."""
        repo_name = os.path.basename(repo_url).replace(".git", "")
        clone_path = os.path.join(MONOREPO_CLONE_DIR, repo_name)
        print(f"Cloning {repo_url} into {clone_path}...")
        try:
            git.Repo.clone_from(repo_url, clone_path)
            print(f"Successfully cloned {repo_name}.")
            return True
        except Exception as e:
            print(f"Error cloning {repo_url}: {e}")
            return False
    
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_clone_one, repo_url): repo_url for repo_url in repos}
        for future in as_completed(futures):
            if future.result():
                cloned_repos_count += 1
    if cloned_repos_count > 0:
        print(f"All repositories cloned. Now indexing the monorepo: {MONOREPO_CLONE_DIR}")
        try:
            index_name = index_repository_direct(
                repo_url=MONOREPO_CLONE_DIR,
                force=True,
                is_local=True,
            )
            print(f"Monorepo indexing completed. Index name: {index_name}")
        except Exception as e:
            print(f"Error indexing monorepo {MONOREPO_CLONE_DIR}: {e}")
    else:
        print("No repositories were successfully cloned, skipping monorepo indexing.")

if __name__ == "__main__":
    github_org = os.getenv("GITHUB_ORG", "plainsightai")
    repo_name_filter = os.getenv("REPO_NAME_FILTER", "")

    preindex_openfilter_repos(org_name=github_org, name_filter=repo_name_filter)
