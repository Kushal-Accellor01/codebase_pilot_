import json
import re
from pathlib import Path
from app.database import GraphDatabase

# Regex patterns for JS/TS
# Matches: require('module') or require("module")
REQUIRE_PATTERN = re.compile(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")

# Matches: import ... from 'module' OR import 'module'
IMPORT_PATTERN = re.compile(r"import\s+(?:.*?\s+from\s+)?['\"]([^'\"]+)['\"]")

# Matches: function myFunc( or class MyClass {
SYMBOL_PATTERN = re.compile(r"(?:export\s+)?(?:default\s+)?(function|class)\s+([a-zA-Z_$][0-9a-zA-Z_$]*)")

# Matches: const myArrowFunc = () =>
ARROW_FUNC_PATTERN = re.compile(r"(?:export\s+)?(?:const|let|var)\s+([a-zA-Z_$][0-9a-zA-Z_$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z_$][0-9a-zA-Z_$]*)\s*=>")

def analyze_js_code(db: GraphDatabase, scan_id: str, repo_root: str):
    cursor = db.conn.cursor()
    
    # Fetch JS and TS files
    cursor.execute(
        "SELECT id, properties FROM nodes WHERE scan_id = ? AND type = 'file'",
        (scan_id,)
    )
    files = cursor.fetchall()
    
    repo_path = Path(repo_root)
    total_symbols = 0
    total_imports = 0

    valid_extensions = {".js", ".jsx", ".ts", ".tsx"}

    for file_row in files:
        props = json.loads(file_row["properties"])
        if props.get("extension") not in valid_extensions:
            continue
            
        file_id = file_row["id"]
        absolute_path = repo_path / props["path"]
        
        try:
            with absolute_path.open("r", encoding="utf-8") as f:
                content = f.read()
                
            # 1. Extract Imports & Requires
            found_modules = set()
            found_modules.update(REQUIRE_PATTERN.findall(content))
            found_modules.update(IMPORT_PATTERN.findall(content))
            
            for mod in found_modules:
                # Basic cleanup to get the root module (e.g., 'lodash/fp' -> 'lodash')
                # Note: internal paths start with '.' or '/'
                root_module = mod.split('/')[0] if not mod.startswith(('.', '/')) else mod
                
                mod_id = f"mod:{root_module}"
                db.add_node(
                    node_id=mod_id,
                    scan_id=scan_id,
                    node_type="module",
                    name=root_module,
                    properties={"is_import": True, "language": "javascript"}
                )
                db.add_edge(source_id=file_id, target_id=mod_id, edge_type="IMPORTS", scan_id=scan_id)
                total_imports += 1

            # 2. Extract Symbols (Functions, Classes, Arrow Functions)
            # We approximate line numbers by finding the string index and counting newlines,
            # but for this fast regex pass, we'll keep it simple without lines first.
            symbols = []
            
            for match in SYMBOL_PATTERN.finditer(content):
                sym_type, sym_name = match.groups()
                symbols.append({"type": sym_type, "name": sym_name})
                
            for match in ARROW_FUNC_PATTERN.finditer(content):
                sym_name = match.group(1)
                symbols.append({"type": "arrow_function", "name": sym_name})

            for sym in symbols:
                sym_id = f"sym:{props['path']}:{sym['name']}"
                db.add_node(
                    node_id=sym_id,
                    scan_id=scan_id,
                    node_type="symbol",
                    name=sym["name"],
                    properties={
                        "file_path": props["path"],
                        "symbol_type": sym["type"],
                        "language": "javascript"
                    }
                )
                db.add_edge(source_id=file_id, target_id=sym_id, edge_type="DEFINES", scan_id=scan_id)
                total_symbols += 1

        except Exception as e:
            print(f"Failed to process {props['path']}: {e}")

    db.commit()
    return total_symbols, total_imports