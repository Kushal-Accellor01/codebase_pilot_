import subprocess
import sys

sys.path.insert(0, "scripts")
import make_sandbox_mirror as mirror  # noqa: E402
from app.orchestrator.ingest import ingest  # noqa: E402
from app.updates import store  # noqa: E402


def test_parse_author():
    assert mirror.parse_author("Codebase Pilot <pilot@localhost>") == ("Codebase Pilot", "pilot@localhost")
    assert mirror.parse_author("just a name") == ("just a name", "pilot@localhost")


def test_mirror_is_a_clean_main_branch_repo_that_ingests(tmp_path):
    dest = mirror.make_mirror(tmp_path / "origin" / "pilot-sandbox-shop")
    branch = subprocess.run(["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    assert branch.stdout.strip() == "main"
    assert not (dest / "node_modules").exists() and (dest / "routes" / "orders.js").exists()
    again = mirror.make_mirror(dest)  # re-creating replaces it
    assert again == dest.resolve()
    scan = ingest(str(dest), str(tmp_path / "t.db"), tmp_path / "ws")
    conn = store.connect(str(tmp_path / "t.db"))
    assert {d.name for d in store.load_inventory(conn, scan)} >= {"express", "mocha", "supertest"}
