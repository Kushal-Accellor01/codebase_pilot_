import ast
import json
from pathlib import Path
from app.database import GraphDatabase

class CodeVisitor(ast.NodeVisitor):
    def __init__(self):
        self.symbols = []
        self.imports = []

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append({
                "module": alias.name,
                "alias": alias.asname,
                "line": node.lineno
            })
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            full_name = f"{module}.{alias.name}" if module else alias.name
            self.imports.append({
                "module": full_name,
                "alias": alias.asname,
                "line": node.lineno
            })
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.symbols.append({
            "type": "function",
            "name": node.name,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno)
        })
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.symbols.append({
            "type": "async_function",
            "name": node.name,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno)
        })
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.symbols.append({
            "type": "class",
            "name": node.name,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno)
        })
        self.generic_visit(node)

def analyze_python_code(db: GraphDatabase, scan_id: str, repo_root: str):
    cursor = db.conn.cursor()
    
    # Fetch only python files
    cursor.execute(
        "SELECT id, properties FROM nodes WHERE scan_id = ? AND type = 'file'",
        (scan_id,)
    )
    files = cursor.fetchall()
    
    repo_path = Path(repo_root)
    total_symbols = 0
    total_imports = 0

    for file_row in files:
        props = json.loads(file_row["properties"])
        if props.get("extension") != ".py":
            continue
            
        file_id = file_row["id"]
        absolute_path = repo_path / props["path"]
        
        try:
            with absolute_path.open("r", encoding="utf-8") as f:
                source_code = f.read()
            
            # Parse code into AST
            tree = ast.parse(source_code, filename=str(absolute_path))
            visitor = CodeVisitor()
            visitor.visit(tree)
            
            # 1. Store Symbols (Functions and Classes) and link them to the File
            for sym in visitor.symbols:
                sym_id = f"sym:{props['path']}:{sym['name']}"
                db.add_node(
                    node_id=sym_id,
                    scan_id=scan_id,
                    node_type="symbol",
                    name=sym["name"],
                    properties={
                        "file_path": props["path"],
                        "symbol_type": sym["type"],
                        "line": sym["line"],
                        "end_line": sym["end_line"]
                    }
                )
                db.add_edge(source_id=file_id, target_id=sym_id, edge_type="DEFINES", scan_id=scan_id)
                total_symbols += 1
            
            # 2. Store Imports and link them to the File, keeping line numbers and imported names
            # root module -> {"lines": [...], "names": [...]}; one IMPORTS edge per (file, module).
            found_modules = {}
            for imp in visitor.imports:
                # We extract the root module (e.g., 'fastapi' from 'fastapi.responses')
                root_module = imp["module"].split(".")[0]
                entry = found_modules.setdefault(root_module, {"lines": set(), "names": set()})
                entry["lines"].add(imp["line"])
                entry["names"].add(imp["module"])

            for root_module, entry in found_modules.items():
                mod_id = f"mod:{root_module}"
                # Add a lightweight module node (the Resolver will link this to real dependencies later)
                db.add_node(
                    node_id=mod_id,
                    scan_id=scan_id,
                    node_type="module",
                    name=root_module,
                    properties={"is_import": True, "language": "python"}
                )
                db.add_edge(
                    source_id=file_id, target_id=mod_id, edge_type="IMPORTS", scan_id=scan_id,
                    properties={"lines": sorted(entry["lines"]), "names": sorted(entry["names"])}
                )
                total_imports += 1

        except SyntaxError as e:
            print(f"Syntax error skipping {props['path']}: {e}")
        except Exception as e:
            print(f"Failed to process {props['path']}: {e}")

    db.commit()
    return total_symbols, total_imports 