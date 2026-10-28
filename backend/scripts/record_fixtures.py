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
    record_sources(headers)
    return 0


def record_sources(headers: dict[str, str]) -> None:
    """deps.dev and GitHub responses for morgan (set GITHUB_TOKEN to avoid the 60/hour anonymous limit)."""
    import os

    out = FIXTURES / "sources"
    out.mkdir(parents=True, exist_ok=True)
    gh_headers = dict(headers)
    if os.environ.get("GITHUB_TOKEN"):
        gh_headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with httpx.Client(headers=gh_headers, timeout=30, follow_redirects=True) as client:
        deps = client.get("https://api.deps.dev/v3/systems/npm/packages/morgan/versions/1.12.1")
        deps.raise_for_status()
        (out / "depsdev_morgan.json").write_text(json.dumps(deps.json(), indent=1), encoding="utf-8")
        repo = client.get("https://api.github.com/repos/expressjs/morgan")
        repo.raise_for_status()
        (out / "github_repo_morgan.json").write_text(json.dumps(repo.json(), indent=1), encoding="utf-8")
        pkg = client.get(
            "https://api.github.com/repos/expressjs/morgan/contents/package.json",
            headers={"Accept": "application/vnd.github.raw"},
        )
        pkg.raise_for_status()
        (out / "github_package_json_morgan.json").write_text(pkg.text, encoding="utf-8")
        print("sources: morgan")


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
