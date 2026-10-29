"""S1: bill of materials from the lockfile, direct and transitive, as `Component`s and CycloneDX 1.6 JSON.

Built in Python from `package-lock.json` / `npm-shrinkwrap.json` (v1, v2 and v3): no network and no npm needed.
`npm sbom --sbom-format cyclonedx --package-lock-only` is an optional cross-check in the tests, not a dependency.
A missing or unreadable lockfile gives no components and a minimal bom; nothing here raises.
"""
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from app.updates.lockfile import LOCKFILES

logger = logging.getLogger(__name__)

Scope = Literal["runtime", "dev", "optional", "peer"]
NODE_MODULES = "node_modules/"
CYCLONEDX_SCOPE = {"runtime": "required", "dev": "excluded", "optional": "optional", "peer": "optional"}


@dataclass
class Component:
    purl: str  # pkg:npm/%40scope/name@1.2.3
    name: str
    version: str
    scope: Scope
    direct: bool
    path: list[str]  # shortest chain from the root manifest: ["express", "body-parser", "qs"]
    location: str = ""  # lockfile key, e.g. node_modules/a/node_modules/b
    depends_on: list[str] = field(default_factory=list)  # purls of what it loads (CycloneDX dependency graph)


def make_purl(name: str, version: str) -> str:
    return f"pkg:npm/{quote(name, safe='/')}@{version}"  # "@scope/name" becomes "%40scope/name"


def name_of(location: str) -> str:
    """`node_modules/a/node_modules/@s/b` -> `@s/b`."""
    return location.rsplit(NODE_MODULES, 1)[-1]


def _v1_to_packages(data: dict) -> dict[str, dict]:
    """Turn a v1 lockfile (nested `dependencies`) into the v2 `packages` shape so one code path handles both."""
    packages: dict[str, dict] = {"": {"name": data.get("name"), "version": data.get("version"),
                                      "dependencies": {n: m.get("version", "*") for n, m in
                                                       (data.get("dependencies") or {}).items() if not m.get("dev")},
                                      "devDependencies": {n: m.get("version", "*") for n, m in
                                                          (data.get("dependencies") or {}).items() if m.get("dev")}}}

    def walk(deps: dict, prefix: str) -> None:
        for name, meta in deps.items():
            if not isinstance(meta, dict):
                continue
            key = f"{prefix}{NODE_MODULES}{name}"
            packages[key] = {
                "version": meta.get("version"), "dev": bool(meta.get("dev")), "optional": bool(meta.get("optional")),
                "dependencies": dict.fromkeys((meta.get("requires") or {}), "*"),
            }
            walk(meta.get("dependencies") or {}, f"{key}/")

    walk(data.get("dependencies") or {}, "")
    return packages


def _resolve(packages: dict[str, dict], parent: str, dep: str) -> str | None:
    """The lockfile key `dep` resolves to from `parent`, the way npm does: nearest node_modules first."""
    base = parent
    while True:
        candidate = f"{base}/{NODE_MODULES}{dep}" if base else f"{NODE_MODULES}{dep}"
        if candidate in packages:
            return candidate
        if not base:
            return None
        cut = base.rfind(f"/{NODE_MODULES}")
        base = base[:cut] if cut != -1 else ""


def _children(meta: dict) -> list[str]:
    names: dict[str, None] = {}
    for field_name in ("dependencies", "optionalDependencies", "peerDependencies", "devDependencies"):
        for name in (meta.get(field_name) or {}):
            names[name] = None
    return list(names)


def _scope(meta: dict) -> Scope:
    if meta.get("dev") or meta.get("devOptional"):
        return "dev"
    if meta.get("optional"):
        return "optional"
    if meta.get("peer"):
        return "peer"
    return "runtime"


def _load(repo_root: Path) -> tuple[dict | None, str | None]:
    path = next((repo_root / n for n in LOCKFILES if (repo_root / n).is_file()), None)
    if path is None:
        return None, "no package-lock.json or npm-shrinkwrap.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path.name} could not be read: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} is not a lockfile"
    return data, None


def build_sbom(repo_root: str | Path) -> tuple[list[Component], dict]:
    """(components, CycloneDX 1.6 document). Never raises: a missing lockfile gives ([], minimal bom)."""
    root = Path(repo_root)
    data, problem = _load(root)
    manifest = _manifest(root)
    if data is None:
        logger.warning("no SBOM for %s: %s", root, problem)
        return [], _bom(manifest, [], warning=problem)
    packages = data.get("packages") if isinstance(data.get("packages"), dict) else _v1_to_packages(data)
    root_meta = packages.get("") or {}
    root_deps = _children(root_meta) or _children(manifest)

    # Breadth-first from the root: the first time a key is reached is its shortest chain.
    chain: dict[str, list[str]] = {}
    edges: dict[str, list[str]] = {}
    queue: deque[str] = deque()
    for dep in root_deps:
        key = _resolve(packages, "", dep)
        if key and key not in chain:
            chain[key] = [name_of(key)]
            queue.append(key)
    while queue:
        key = queue.popleft()
        for dep in _children(packages[key]):
            child = _resolve(packages, key, dep)
            if child is None:
                continue
            edges.setdefault(key, []).append(child)
            if child not in chain:
                chain[child] = [*chain[key], name_of(child)]
                queue.append(child)

    direct = {name_of(k) for k in (_resolve(packages, "", d) for d in root_deps) if k}
    components: list[Component] = []
    for key, meta in packages.items():
        if key == "" or not key.startswith(NODE_MODULES) or not isinstance(meta, dict) or not meta.get("version"):
            continue
        name = meta.get("name") or name_of(key)
        version = meta["version"]
        top_level = key == f"{NODE_MODULES}{name_of(key)}"
        components.append(Component(
            purl=make_purl(name, version), name=name, version=version, scope=_scope(meta),
            direct=top_level and name_of(key) in direct, path=chain.get(key) or [name], location=key,
            depends_on=[make_purl(packages[c].get("name") or name_of(c), packages[c]["version"])
                        for c in edges.get(key, []) if packages[c].get("version")],
        ))
    components.sort(key=lambda c: (c.name, c.version, c.location))
    return components, _bom(manifest, components, root_deps=root_deps, packages=packages)


def _manifest(root: Path) -> dict:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _bom(manifest: dict, components: list[Component], *, warning: str | None = None,
         root_deps: list[str] | None = None, packages: dict | None = None) -> dict:
    name = manifest.get("name") or "unnamed-project"
    version = manifest.get("version") or "0.0.0"
    root_ref = f"root:{name}@{version}"
    unique: dict[str, Component] = {}
    for c in components:  # CycloneDX lists each name@version once, even when nested copies exist
        unique.setdefault(c.purl, c)
    seed = json.dumps(sorted(unique)).encode()
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"codebase-pilot:{name}:{version}:{seed.decode()[:4000]}")
    depends: dict[str, set[str]] = {p: set() for p in unique}
    for c in components:
        depends[c.purl].update(d for d in c.depends_on if d in unique)
    root_children = sorted({make_purl((packages or {}).get(k, {}).get("name") or name_of(k), v["version"])
                            for dep in (root_deps or [])
                            for k in [_resolve(packages or {}, "", dep)] if k
                            for v in [(packages or {}).get(k, {})] if v.get("version")} & set(unique))
    bom: dict = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": {"components": [{"type": "application", "name": "codebase-pilot"}]},
            "component": {"type": "application", "bom-ref": root_ref, "name": name, "version": version},
        },
        "components": [
            {
                "type": "library", "bom-ref": c.purl, "name": c.name, "version": c.version, "purl": c.purl,
                "scope": CYCLONEDX_SCOPE[c.scope],
                "properties": [{"name": "pilot:scope", "value": c.scope},
                               {"name": "pilot:direct", "value": str(c.direct).lower()}],
            }
            for c in sorted(unique.values(), key=lambda c: (c.name, c.version))
        ],
        "dependencies": [{"ref": root_ref, "dependsOn": root_children}]
        + [{"ref": ref, "dependsOn": sorted(deps)} for ref, deps in sorted(depends.items())],
    }
    if warning:
        bom["metadata"]["properties"] = [{"name": "pilot:warning", "value": warning}]
    return bom


def write_sbom(bom: dict, directory: str | Path, scan_id: str) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{scan_id}.cdx.json"
    path.write_text(json.dumps(bom, indent=2), encoding="utf-8")
    return path
