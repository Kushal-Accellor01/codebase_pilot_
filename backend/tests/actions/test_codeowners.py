from pathlib import Path

import pytest

from app.actions.codeowners import load_codeowners, owners_by_file, owners_for, parse_codeowners, team_for

SANDBOX = Path(__file__).resolve().parents[3] / "pilot-sandbox-shop"

RULES = parse_codeowners("""
# comment
*                     @org/default
*.js                  @org/js
/docs/                @org/docs
docs/*.md             @org/writers
/build/logs/          @org/ops
apps/                 @org/apps @alice
**/migrations/**      @org/db
/src/api              @org/api
README.md             @org/readme   # trailing comment
/lib/*.ts
""")


def test_parse_skips_comments_and_keeps_multiple_owners():
    assert ("apps/", ["@org/apps", "@alice"]) in RULES
    assert ("README.md", ["@org/readme"]) in RULES
    assert ("/lib/*.ts", []) in RULES  # a rule with no owner clears ownership
    assert all(not p.startswith("#") for p, _ in RULES)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("unknown.txt", ["@org/default"]),  # '*' matches everything
        ("src/a.js", ["@org/js"]),  # later rule wins over '*'
        ("deep/er/b.js", ["@org/js"]),  # unanchored '*.js' matches at any depth
        ("docs/guide.md", ["@org/writers"]),  # docs/*.md is later than /docs/
        ("docs/sub/guide.md", ["@org/docs"]),  # '*' in docs/*.md does not cross directories
        ("build/logs/x.txt", ["@org/ops"]),
        ("other/build/logs/x.txt", ["@org/default"]),  # '/build/logs/' is anchored to the root
        ("apps/web/index.js", ["@org/apps", "@alice"]),  # apps/ is later than *.js, so it wins
        ("apps/web/readme.txt", ["@org/apps", "@alice"]),
        ("db/migrations/001.sql", ["@org/db"]),
        ("migrations/001.sql", ["@org/db"]),  # '**/' also matches zero directories
        ("src/api/handler.txt", ["@org/api"]),  # '/src/api' matches the directory without a trailing slash
        ("x/README.md", ["@org/readme"]),
        ("lib/types.ts", []),
        ("lib/sub/types.ts", ["@org/default"]),
    ],
)
def test_owners_for(path, expected):
    assert owners_for(path, RULES) == expected


def test_windows_separators_and_leading_slash_are_normalised():
    assert owners_for("docs\\sub\\guide.md", RULES) == ["@org/docs"]
    assert owners_for("/src/a.js", RULES) == ["@org/js"]


def test_no_rules_means_no_owner():
    assert owners_for("a.js", []) == [] and load_codeowners(None) == []


def test_load_prefers_github_dir_then_root_then_docs(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CODEOWNERS").write_text("* @docs-owner\n", encoding="utf-8")
    assert load_codeowners(tmp_path) == [("*", ["@docs-owner"])]
    (tmp_path / "CODEOWNERS").write_text("* @root-owner\n", encoding="utf-8")
    assert load_codeowners(tmp_path) == [("*", ["@root-owner"])]
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text("* @gh-owner\n", encoding="utf-8")
    assert load_codeowners(tmp_path) == [("*", ["@gh-owner"])]


def test_sandbox_maps_every_file_to_team_shop():
    rules = load_codeowners(SANDBOX)
    files = ["app.js", "routes/orders.js", "routes/admin.js", "lib/notify.js", "test/orders.test.js", "package.json"]
    assert all(v == ["@acme/team-shop"] for v in owners_by_file(files, rules).values())
    assert team_for(files, rules) == "@acme/team-shop"


def test_team_for_picks_the_majority_owner():
    assert team_for(["src/a.js", "src/b.js", "docs/x.md"], RULES) == "@org/js"
    assert team_for(["a"], []) is None
