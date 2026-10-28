import os
import json
import posixpath
from app.database import GraphDatabase

def resolve_imports(db: GraphDatabase, scan_id: str):
    cursor = db.conn.cursor()
    
    # 1. Build a map of all internal files
    cursor.execute("SELECT id, properties FROM nodes WHERE scan_id = ? AND type = 'file'", (scan_id,))
    file_nodes = cursor.fetchall()
    
    file_map = {}
    for row in file_nodes:
        props = json.loads(row["properties"])
        # Normalize slashes for cross-platform consistency
        path_str = props.get("path", "").replace("\\", "/") 
        file_map[path_str] = row["id"]
        
    # 2. Get all known external dependencies
    cursor.execute("SELECT id, name FROM nodes WHERE scan_id = ? AND type = 'dependency'", (scan_id,))
    deps = {row["name"].lower(): row["id"] for row in cursor.fetchall()}
    
    # 3. Get all module imports WITH their source file context
    cursor.execute("""
        SELECT m.id AS mod_id, m.name AS mod_name, json_extract(m.properties, '$.language') AS lang,
               json_extract(m.properties, '$.resolved_path') AS resolved_path,
               f.id AS file_id, json_extract(f.properties, '$.path') AS source_path
        FROM nodes m
        JOIN edges i ON m.id = i.target_id AND i.type = 'IMPORTS' AND i.scan_id = m.scan_id
        JOIN nodes f ON i.source_id = f.id AND f.scan_id = m.scan_id
        WHERE m.scan_id = ? AND m.type = 'module'
    """, (scan_id,))
    imports = cursor.fetchall()
    
    resolved_ext = 0
    resolved_int = 0
    
    for imp in imports:
        mod_id = imp["mod_id"]
        mod_name = imp["mod_name"]
        lang = imp["lang"]
        source_path = imp["source_path"].replace("\\", "/") if imp["source_path"] else ""
        
        # --- External Dependency Resolution ---
        if mod_name.lower() in deps:
            db.add_edge(mod_id, deps[mod_name.lower()], "RESOLVES_TO", scan_id)
            resolved_ext += 1
            continue
            
        # --- Internal File Resolution ---
        target_file_id = None
        
        if lang == "javascript" and imp["resolved_path"] is not None:
            # Relative import already resolved against the importing file's folder by the analyzer.
            base = imp["resolved_path"]
            candidates = [base + ext for ext in [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ""]]
            candidates += [posixpath.join(base, name) for name in ["index.js", "index.ts"]]
            for test_path in candidates:
                if test_path in file_map:
                    target_file_id = file_map[test_path]
                    break

        elif lang == "javascript" and mod_name.startswith((".", "/")):
            # Resolve relative JS path based on the file making the import
            base_dir = os.path.dirname(source_path)
            resolved_base = os.path.normpath(os.path.join(base_dir, mod_name)).replace("\\", "/")
            
            # Check common JS/TS extensions and index resolutions
            for ext in [".js", ".jsx", ".ts", ".tsx", "/index.js", "/index.ts"]:
                test_path = resolved_base + ext if not ext.startswith("/") else resolved_base + ext
                if test_path in file_map:
                    target_file_id = file_map[test_path]
                    break
                    
        elif lang == "python":
            # Convert python dot notation to file path (app.database -> app/database.py)
            resolved_base = mod_name.replace(".", "/")
            for ext in [".py", "/__init__.py"]:
                test_path = resolved_base + ext
                if test_path in file_map:
                    target_file_id = file_map[test_path]
                    break
                    
        # Draw the edge if an internal file was matched
        if target_file_id:
            db.add_edge(mod_id, target_file_id, "RESOLVES_TO", scan_id)
            resolved_int += 1

    db.commit()
    return resolved_ext, resolved_int