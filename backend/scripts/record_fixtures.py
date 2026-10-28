"""Record real registry responses once into tests/fixtures/ (tests never touch the network).

Run from backend/:  python scripts/record_fixtures.py
"""
import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

NPM_PACKAGES = ["morgan", "after", "left-pad", "@tootallnate/once"]
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUT = FIXTURES / "npm"

# OSV: a vulnerable version (minimist 1.2.0 has two advisories) and a clean one.
OSV_QUERIES = [("minimist", "1.2.0"), ("morgan", "1.12.1")]


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
    record_osv(headers)
    return 0


def record_osv(headers: dict[str, str]) -> None:
    out = FIXTURES / "osv"
    out.mkdir(parents=True, exist_ok=True)
    body = {"queries": [{"package": {"name": n, "ecosystem": "npm"}, "version": v} for n, v in OSV_QUERIES]}
    with httpx.Client(headers=headers, timeout=30) as client:
        batch = client.post("https://api.osv.dev/v1/querybatch", json=body)
        batch.raise_for_status()
        (out / "querybatch.json").write_text(json.dumps(batch.json(), indent=1), encoding="utf-8")
        for result in batch.json()["results"]:
            for vuln in result.get("vulns", []):
                resp = client.get(f"https://api.osv.dev/v1/vulns/{vuln['id']}")
                resp.raise_for_status()
                (out / f"{vuln['id']}.json").write_text(json.dumps(resp.json(), indent=1), encoding="utf-8")
                print(f"osv {vuln['id']}")


if __name__ == "__main__":
    sys.exit(main())
