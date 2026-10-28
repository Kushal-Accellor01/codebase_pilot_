import json
import posixpath
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

def package_root(specifier: str) -> str:
    """'lodash/fp' -> 'lodash', '@babel/core/lib/x' -> '@babel/core'; relative paths are kept as-is."""
    if specifier.startswith((".", "/")):
        return specifier
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0]

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
                
            # 1. Extract Imports & Requires, with line numbers
            # root module -> {"lines": [...], "specifiers": [...]}; one IMPORTS edge per (file, module).
            found_modules = {}
            for pattern in (REQUIRE_PATTERN, IMPORT_PATTERN):
                for match in pattern.finditer(content):
                    mod = match.group(1)
                    line = content.count("\n", 0, match.start()) + 1
                    entry = found_modules.setdefault(package_root(mod), {"lines": set(), "specifiers": set()})
                    entry["lines"].add(line)
                    entry["specifiers"].add(mod)

            file_dir = posixpath.dirname(props["path"].replace("\\", "/"))
            for root_module, entry in found_modules.items():
                properties = {"is_import": True, "language": "javascript"}
                if root_module.startswith("."):
                    # './user' means a different file in every folder: key the module by the
                    # repo-relative path it points to, so it can't resolve to another folder's file.
                    resolved = posixpath.normpath(posixpath.join(file_dir, root_module))
                    resolved = "" if resolved == "." else resolved
                    mod_id = f"mod:rel:{resolved}"
                    properties.update({"relative": True, "resolved_path": resolved})
                else:
                    mod_id = f"mod:{root_module}"
                db.add_node(
                    node_id=mod_id,
                    scan_id=scan_id,
                    node_type="module",
                    name=root_module,
                    properties=properties
                )
                db.add_edge(
                    source_id=file_id, target_id=mod_id, edge_type="IMPORTS", scan_id=scan_id,
                    properties={"lines": sorted(entry["lines"]), "specifiers": sorted(entry["specifiers"])}
                )
                total_imports += 1

            # 2. Extract Symbols (Functions, Classes, Arrow Functions), with line numbers
            symbols = []

            for match in SYMBOL_PATTERN.finditer(content):
                sym_type, sym_name = match.groups()
                symbols.append({"type": sym_type, "name": sym_name,
                                "line": content.count("\n", 0, match.start()) + 1})

            for match in ARROW_FUNC_PATTERN.finditer(content):
                sym_name = match.group(1)
                symbols.append({"type": "arrow_function", "name": sym_name,
                                "line": content.count("\n", 0, match.start()) + 1})

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
                        "language": "javascript",
                        "line": sym["line"]
                    }
                )
                db.add_edge(source_id=file_id, target_id=sym_id, edge_type="DEFINES", scan_id=scan_id)
                total_symbols += 1

        except Exception as e:
            print(f"Failed to process {props['path']}: {e}")

    db.commit()
    return total_symbols, total_imports