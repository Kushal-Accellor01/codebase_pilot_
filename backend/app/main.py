import subprocess
import tempfile
import json
from pathlib import Path

from app.database import GraphDatabase
from app.scanner import scan_repository
from app.dependency_extractor import extract_dependencies
from app.python_analyzer import analyze_python_code
from app.js_analyzer import analyze_js_code
from app.resolver import resolve_imports

def clone_github_repo(repo_url: str, target_dir: str):
    print(f"Cloning {repo_url} into {target_dir}...")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, target_dir],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Git clone failed: {result.stderr}")

def process_github_url(github_url: str):
    db = GraphDatabase("codebase_pilot.db")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_name = github_url.rstrip("/").split("/")[-1].replace(".git", "")
        repo_path = Path(temp_dir) / repo_name
        
        clone_github_repo(github_url, str(repo_path))
        
        commit_result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True
        )
        commit_sha = commit_result.stdout.strip() if commit_result.returncode == 0 else "HEAD"
        
        # 1 & 2. Inventory and Dependencies
        scan_id = scan_repository(str(repo_path), db, commit_sha)
        deps_count = extract_dependencies(db, scan_id, str(repo_path))
        
        # 3. Source Code Analysis
        py_sym, py_imp = analyze_python_code(db, scan_id, str(repo_path))
        js_sym, js_imp = analyze_js_code(db, scan_id, str(repo_path))
        
        # 4. Resolve Graph Edges (Both Internal and External)
        resolved_ext, resolved_int = resolve_imports(db, scan_id)
        
        # --- Scan Summary ---
        cursor = db.conn.cursor()
        cursor.execute("SELECT count(*) FROM nodes WHERE scan_id = ? AND type = 'file'", (scan_id,))
        file_count = cursor.fetchone()[0]

        print(f"\n--- Scan Summary ---")
        print(f"Repository: {github_url}")
        print(f"Commit: {commit_sha}")
        print(f"Indexed {file_count} files.")
        print(f"Extracted {deps_count} declared dependencies.")
        print(f"Resolved {resolved_ext} imports to external dependencies.")
        print(f"Resolved {resolved_int} imports to internal files.")
        
        # --- 5A. Print Declared Dependencies (From Manifests) ---
        print(f"\n--- 1. Declared Dependencies ---")
        cursor.execute("""
            SELECT n.name as dep_name, n.properties as dep_props, 
                   e.properties as edge_props, f.properties as file_props
            FROM nodes n
            JOIN edges e ON n.id = e.target_id AND e.scan_id = n.scan_id
            JOIN nodes f ON e.source_id = f.id AND f.scan_id = n.scan_id
            WHERE n.scan_id = ? AND n.type = 'dependency' AND e.type = 'DECLARES'
        """, (scan_id,))
        
        declared_deps = cursor.fetchall()
        
        if declared_deps:
            deps_by_file = {}
            for row in declared_deps:
                file_path = json.loads(row["file_props"]).get("path", "Unknown")
                if file_path not in deps_by_file:
                    deps_by_file[file_path] = []
                deps_by_file[file_path].append(row)
                
            for file_path, deps in deps_by_file.items():
                print(f"\n📂 Declared in {file_path}:")
                for row in sorted(deps, key=lambda x: x["dep_name"]):
                    dep_name = row["dep_name"]
                    dep_props = json.loads(row["dep_props"])
                    edge_props = json.loads(row["edge_props"])
                    
                    ecosystem = dep_props.get("ecosystem", "Unknown")
                    constraint = edge_props.get("declared_constraint", "")
                    
                    constraint_str = f" @ {constraint}" if constraint else ""
                    print(f"  └─ [{ecosystem}] {dep_name}{constraint_str}")
        else:
            print("No dependencies found in recognized manifest files.")

        # --- 5B. Print External Dependency Usage Mapping (From Source Code) ---
        print(f"\n--- 2. External Dependency Usage Mapping ---")
        cursor.execute("""
            SELECT 
                d.name AS Dependency, 
                json_extract(f.properties, '$.path') AS Used_In_File
            FROM nodes d
            JOIN edges r ON d.id = r.target_id AND r.type = 'RESOLVES_TO' AND r.scan_id = d.scan_id
            JOIN nodes m ON r.source_id = m.id AND m.scan_id = d.scan_id
            JOIN edges i ON m.id = i.target_id AND i.type = 'IMPORTS' AND i.scan_id = d.scan_id
            JOIN nodes f ON i.source_id = f.id AND f.scan_id = d.scan_id
            WHERE d.type = 'dependency' AND d.scan_id = ?
            ORDER BY Dependency, Used_In_File
        """, (scan_id,))
        
        usages = cursor.fetchall()
        
        if usages:
            usage_map = {}
            for row in usages:
                dep = row["Dependency"]
                file_path = row["Used_In_File"]
                if dep not in usage_map:
                    usage_map[dep] = set()
                usage_map[dep].add(file_path)
                
            for dep, files in sorted(usage_map.items()):
                print(f"\n📦 {dep} is used in:")
                for file_path in sorted(files):
                    print(f"  └─ 📄 {file_path}")
        else:
            print("No mapped usages found.")

        # --- 5C. Print Internal Repository Graph (File-to-File Relationships) ---
        print(f"\n--- 3. Internal Repository Graph ---")
        cursor.execute("""
            SELECT 
                json_extract(f1.properties, '$.path') AS Importer,
                json_extract(f2.properties, '$.path') AS Imported
            FROM nodes m
            JOIN edges i ON m.id = i.target_id AND i.type = 'IMPORTS' AND i.scan_id = m.scan_id
            JOIN nodes f1 ON i.source_id = f1.id AND f1.scan_id = m.scan_id
            JOIN edges r ON m.id = r.source_id AND r.type = 'RESOLVES_TO' AND r.scan_id = m.scan_id
            JOIN nodes f2 ON r.target_id = f2.id AND f2.scan_id = m.scan_id
            WHERE m.type = 'module' AND f2.type = 'file' AND m.scan_id = ?
            ORDER BY Importer, Imported
        """, (scan_id,))
        
        internal_links = cursor.fetchall()
        
        if internal_links:
            graph_map = {}
            for row in internal_links:
                importer = row["Importer"]
                imported = row["Imported"]
                if importer not in graph_map:
                    graph_map[importer] = set()
                graph_map[importer].add(imported)
                
            for importer, imports in sorted(graph_map.items()):
                print(f"\n📄 {importer} depends on:")
                for imported in sorted(imports):
                    print(f"  └─ 📄 {imported}")
        else:
            print("No internal file dependencies found.")
            
    db.close()

if __name__ == "__main__":
    target_url = "https://github.com/expressjs/express" 
    process_github_url(target_url)