import hashlib
from pathlib import Path
from app.database import GraphDatabase

IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules",
    "__pycache__", "dist", "build", ".next", ".pytest_cache"
}

MANIFESTS = {
    # Python
    "requirements.txt", "pyproject.toml", "Pipfile", "poetry.lock", "uv.lock",
    # JavaScript / Node
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    # Java
    "pom.xml", "build.gradle", "build.gradle.kts",
    # C/C++
    "CMakeLists.txt", "vcpkg.json", "conanfile.txt"
}

def sha256_file(path: Path) -> str:
    """Safely hash a file in chunks."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return "unhashable"

def scan_repository(repo_path: str, db: GraphDatabase, commit_sha: str = "HEAD",
                    repo_url: str = None, branch: str = None):
    root_path = Path(repo_path).resolve()
    
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"Invalid repository path: {repo_path}")

    # Initialize the scan in the database
    scan_id = db.create_scan(str(root_path), commit_sha, repo_url=repo_url, branch=branch)
    
    # Add the root directory as the first node
    root_node_id = f"dir:."
    db.add_node(
        node_id=root_node_id,
        scan_id=scan_id,
        node_type="directory",
        name=root_path.name,
        properties={"path": "."}
    )

    for path in sorted(root_path.rglob("*")):
        relative_path = path.relative_to(root_path)
        # Always store POSIX paths ('lib/router.js'), so ids match on Windows, macOS and Linux.
        rel = relative_path.as_posix()
        
        # Skip ignored directories
        if any(part in IGNORED_DIRS for part in relative_path.parts):
            continue
            
        if path.is_symlink():
            continue

        parent_rel = relative_path.parent.as_posix()
        parent_id = f"dir:{parent_rel}" if parent_rel != "." else root_node_id

        if path.is_dir():
            dir_id = f"dir:{rel}"
            db.add_node(
                node_id=dir_id,
                scan_id=scan_id,
                node_type="directory",
                name=path.name,
                properties={"path": rel}
            )
            # Link parent dir -> contains -> this dir
            db.add_edge(parent_id, dir_id, "CONTAINS", scan_id)

        elif path.is_file():
            file_id = f"file:{rel}"
            is_manifest = path.name in MANIFESTS
            
            db.add_node(
                node_id=file_id,
                scan_id=scan_id,
                node_type="file",
                name=path.name,
                properties={
                    "path": rel,
                    "extension": path.suffix,
                    "is_manifest": is_manifest,
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path)
                }
            )
            # Link parent dir -> contains -> this file
            db.add_edge(parent_id, file_id, "CONTAINS", scan_id)

    db.commit()
    print(f"Scan complete. Scan ID: {scan_id}")
    return scan_id

# --- Test the Vertical Slice ---
if __name__ == "__main__":
    import os
    
    # Let's scan the backend directory itself as a test
    target_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    db = GraphDatabase("test_graph.db")
    scan_repository(target_repo, db)
    
    # Verify the data
    cursor = db.conn.cursor()
    cursor.execute("SELECT count(*) FROM nodes WHERE type='file'")
    file_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT count(*) FROM edges WHERE type='CONTAINS'")
    edge_count = cursor.fetchone()[0]
    
    print(f"Database populated with {file_count} files and {edge_count} structural edges.")
    db.close()