"""Record real registry responses once into tests/fixtures/ (tests never touch the network).

Run from backend/:  python scripts/record_fixtures.py
"""
import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

NPM_PACKAGES = ["morgan", "after", "left-pad", "@tootallnate/once"]
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "npm"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "codebase-pilot/0.1 (fixture recorder)"}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for name in NPM_PACKAGES:
            resp = client.get(f"https://registry.npmjs.org/{quote(name, safe='@')}")
            resp.raise_for_status()
            path = OUT / (name.replace("/", "__") + ".json")
            path.write_text(json.dumps(resp.json(), indent=1), encoding="utf-8")
            print(f"{name}: {len(resp.content)} bytes -> {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
