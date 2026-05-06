#!/usr/bin/env python3
"""Look up the version that a DockerHub :<tag> currently points to.

Fetches the digest of `<repo>:<latest-tag>`, scans the tag list for sibling
tags pointing at the same digest, filters to ones ending with `--suffix` whose
stem (minus 'v' and minus suffix) is pure X.Y.Z, prints the highest such
version. Prints `0.0.0` if nothing matches or DockerHub is unreachable, so
callers can compare unconditionally.

Stdlib-only (urllib + json) — runs in `gcr.io/google.com/cloudsdktool/cloud-sdk`
without any extra installs.
"""
import argparse
import json
import sys
import urllib.request

# Hardcoded — Cloud Build only ever calls this for openfilter-mcp's release-tag
# pipeline. Lift to an arg if a second repo ever needs the same lookup.
REPO = "plainsightai/openfilter-mcp"


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def lookup(latest_tag: str, suffix: str) -> str:
    base = f"https://hub.docker.com/v2/repositories/{REPO}"

    try:
        digest = (_fetch_json(f"{base}/tags/{latest_tag}").get("images") or [{}])[0].get("digest", "")
    except Exception:
        return "0.0.0"
    if not digest:
        return "0.0.0"

    try:
        tags = _fetch_json(f"{base}/tags?page_size=100").get("results", [])
    except Exception:
        return "0.0.0"

    versions: list[tuple[int, int, int]] = []
    for tag in tags:
        if not any(img.get("digest") == digest for img in tag.get("images", [])):
            continue
        name = tag.get("name", "")
        if not name.endswith(suffix):
            continue
        stem = name.removeprefix("v").removesuffix(suffix)
        parts = stem.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            continue
        versions.append(tuple(int(p) for p in parts))  # type: ignore[arg-type]

    if not versions:
        return "0.0.0"
    return ".".join(str(p) for p in max(versions))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--latest-tag", required=True, help="e.g. latest or latest-slim")
    p.add_argument("--suffix", default="", help="e.g. -slim (empty for the GPU build)")
    args = p.parse_args()
    print(lookup(args.latest_tag, args.suffix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
