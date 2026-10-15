"""Is the local Ollama server reachable, and does it have the model?

Used by scripts/local_up.sh before the stack starts. A missing model is
downloaded through the server's own /api/pull: a download only, never a load
or an unload, so other work on the server is not disturbed.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", required=True)
    args = ap.parse_args(argv)
    base = args.url.rstrip("/")
    try:
        tags = httpx.get(f"{base}/api/tags", timeout=10.0).json()
    except (httpx.HTTPError, ValueError) as exc:
        sys.stderr.write(f"no Ollama server answers at {base} ({exc}); start Ollama, then run make up again\n")
        return 1
    names = {m.get("name") for m in tags.get("models", []) if isinstance(m, dict)}
    if args.model in names:
        return 0
    sys.stdout.write(f"downloading {args.model} into the local Ollama (download only, nothing is loaded)\n")
    with httpx.stream("POST", f"{base}/api/pull", json={"model": args.model}, timeout=None) as resp:
        for line in resp.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            if event.get("error"):
                sys.stderr.write(f"pull failed: {event['error']}\n")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
