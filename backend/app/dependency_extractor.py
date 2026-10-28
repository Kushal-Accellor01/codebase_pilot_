import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
try:
    import tomllib  # Python 3.11+
except ImportError:
    pass

from app.database import GraphDatabase

# --- PYTHON ---
def parse_requirements_txt(file_path: Path) -> list:
    deps = []
    try:
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"^([a-zA-Z0-9_\-]+)(.*)", line)
                if match:
                    deps.append({"name": match.group(1), "constraint": match.group(2).strip(), "ecosystem": "PyPI"})
    except Exception:
        pass
    return deps

def parse_pyproject_toml(file_path: Path) -> list:
    deps = []
    if 'tomllib' not in globals():
        return deps
    try:
        with file_path.open("rb") as f:
            data = tomllib.load(f)
            project_deps = data.get("project", {}).get("dependencies", [])
            for dep in project_deps:
                match = re.match(r"^([a-zA-Z0-9_\-]+)(.*)", dep)
                if match:
                    deps.append({"name": match.group(1), "constraint": match.group(2).strip(), "ecosystem": "PyPI"})
    except Exception:
        pass
    return deps

# --- JAVASCRIPT ---
def parse_package_json(file_path: Path) -> list:
    deps = []
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            for section in ["dependencies", "devDependencies", "peerDependencies"]:
                for name, version in data.get(section, {}).items():
                    deps.append({"name": name, "constraint": version, "ecosystem": "npm"})
    except Exception:
        pass
    return deps

# --- JAVA ---
def parse_pom_xml(file_path: Path) -> list:
    deps = []
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        # Handle namespaces by matching local tags
        for elem in root.iter():
            if elem.tag.endswith('dependency'):
                group_id = elem.findtext('*[local-name()="groupId"]')
                artifact_id = elem.findtext('*[local-name()="artifactId"]')
                version = elem.findtext('*[local-name()="version"]')
                if artifact_id:
                    name = f"{group_id}:{artifact_id}" if group_id else artifact_id
                    deps.append({"name": name, "constraint": version or "", "ecosystem": "Maven"})
    except Exception:
        pass
    return deps

def parse_build_gradle(file_path: Path) -> list:
    deps = []
    try:
        with file_path.open("r", encoding="utf-8") as f:
            content = f.read()
            # Matches: implementation 'group:name:version' or implementation("group:name:version")
            pattern = r"(?:implementation|compile|api|testImplementation)\s*\(*['\"]([^:]+):([^:]+):([^'\"]+)['\"]\)*"
            matches = re.findall(pattern, content)
            for group, name, version in matches:
                deps.append({"name": f"{group}:{name}", "constraint": version, "ecosystem": "Gradle"})
    except Exception:
        pass
    return deps

# --- C/C++ ---
def parse_cmake_lists(file_path: Path) -> list:
    deps = []
    try:
        with file_path.open("r", encoding="utf-8") as f:
            content = f.read()
            # Matches: find_package(Boost 1.70 REQUIRED)
            pattern = r"find_package\s*\(\s*([A-Za-z0-9_]+)(.*?)\)"
            matches = re.findall(pattern, content)
            for name, rest in matches:
                deps.append({"name": name, "constraint": rest.strip(), "ecosystem": "CMake"})
    except Exception:
        pass
    return deps

def parse_vcpkg_json(file_path: Path) -> list:
    deps = []
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            for dep in data.get("dependencies", []):
                if isinstance(dep, str):
                    deps.append({"name": dep, "constraint": "", "ecosystem": "vcpkg"})
                elif isinstance(dep, dict) and "name" in dep:
                    constraint = dep.get("version>=", dep.get("version", ""))
                    deps.append({"name": dep["name"], "constraint": constraint, "ecosystem": "vcpkg"})
    except Exception:
        pass
    return deps

# --- ROUTER ---
# ... (keep all the parse_* functions the same)

def extract_dependencies(db: GraphDatabase, scan_id: str, repo_root: str):
    cursor = db.conn.cursor()
    cursor.execute(
        "SELECT id, name, properties FROM nodes WHERE scan_id = ? AND type = 'file'",
        (scan_id,)
    )
    files = cursor.fetchall()
    
    repo_path = Path(repo_root)
    total_deps = 0
    
    for file_row in files:
        props = json.loads(file_row["properties"])
        if not props.get("is_manifest"):
            continue
            
        file_id = file_row["id"]
        file_name = file_row["name"]
        absolute_path = repo_path / props["path"]
        
        deps = []
        if file_name == "requirements.txt":
            deps = parse_requirements_txt(absolute_path)
        elif file_name == "pyproject.toml":
            deps = parse_pyproject_toml(absolute_path)
        elif file_name == "package.json":
            deps = parse_package_json(absolute_path)
        # ... (other parsers remain the same)
            
        for dep in deps:
            dep_name = dep["name"]
            dep_id = f"dep:{dep['ecosystem'].lower()}:{dep_name.lower()}"
            
            # The Node just represents the library concept itself
            db.add_node(
                node_id=dep_id,
                scan_id=scan_id,
                node_type="dependency",
                name=dep_name,
                properties={"ecosystem": dep["ecosystem"]}
            )
            
            # The Edge stores the specific file and version requirement!
            db.add_edge(
                source_id=file_id,
                target_id=dep_id,
                edge_type="DECLARES",
                scan_id=scan_id,
                properties={"declared_constraint": dep["constraint"]}
            )
            total_deps += 1
            
    db.commit()
    return total_deps