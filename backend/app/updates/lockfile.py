"""Exact installed versions from package-lock.json / npm-shrinkwrap.json (A5)."""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json")


def load_lockfile_versions(repo_root: str | Path | None) -> dict[str, str]:
    """{package name: installed version} for top-level packages. Missing or invalid file -> {}.

    v2/v3 lockfiles list `packages["node_modules/<name>"]`; nested copies (`node_modules/a/node_modules/b`) are
    not what the project declared and are ignored. v1 lockfiles use `dependencies[name].version`.
    """
    if repo_root is None:
        return {}
    path = next((Path(repo_root) / n for n in LOCKFILES if (Path(repo_root) / n).is_file()), None)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}

    versions: dict[str, str] = {}
    packages = data.get("packages")
    if isinstance(packages, dict):
        prefix = "node_modules/"
        for key, meta in packages.items():
            if not key.startswith(prefix) or "/node_modules/" in key[len(prefix) - 1:] or not isinstance(meta, dict):
                continue
            if meta.get("version"):
                versions[key[len(prefix):]] = meta["version"]
    elif isinstance(data.get("dependencies"), dict):
        for name, meta in data["dependencies"].items():
            if isinstance(meta, dict) and meta.get("version"):
                versions[name] = meta["version"]
    return versions
