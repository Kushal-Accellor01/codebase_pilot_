from pathlib import Path

from app.orchestrator.config_loader import RepoPilotConfig, load_repo_config

SANDBOX = Path(__file__).resolve().parents[3] / "pilot-sandbox-shop"


def write(tmp_path, text):
    (tmp_path / ".codebase-pilot.yml").write_text(text, encoding="utf-8")
    return tmp_path


def test_the_sandbox_file_with_a_string_command():
    config, warnings = load_repo_config(SANDBOX)
    assert warnings == [] and config.mode == "assisted" and config.test_command == ["npm", "test"]


def test_missing_file_gives_defaults_without_warning(tmp_path):
    config, warnings = load_repo_config(tmp_path)
    assert config == RepoPilotConfig() and warnings == []
    assert load_repo_config(None) == (RepoPilotConfig(), [])


def test_nested_keys_are_flattened(tmp_path):
    config, warnings = load_repo_config(write(tmp_path, """
mode: advisory
service: shop-api
owners: "@acme/team-shop"
notify:
  min_severity: high
  min_confidence: 0.7
fixes:
  allowed_paths: ["routes/**", "lib/**"]
  test_command: "npm run test:ci -- --bail"
"""))
    assert warnings == []
    assert (config.mode, config.service, config.owners) == ("advisory", "shop-api", "@acme/team-shop")
    assert (config.min_severity, config.min_confidence) == ("high", 0.7)
    assert config.allowed_paths == ["routes/**", "lib/**"]
    assert config.test_command == ["npm", "run", "test:ci", "--", "--bail"]


def test_list_command_is_accepted(tmp_path):
    config, _ = load_repo_config(write(tmp_path, "test_command: [yarn, test]\n"))
    assert config.test_command == ["yarn", "test"]


def test_invalid_yaml_falls_back_with_a_warning(tmp_path):
    config, warnings = load_repo_config(write(tmp_path, "mode: [unclosed\n"))
    assert config == RepoPilotConfig() and "ignored, using defaults" in warnings[0]


def test_unknown_mode_falls_back_with_a_warning(tmp_path):
    config, warnings = load_repo_config(write(tmp_path, "mode: yolo\n"))
    assert config.mode == "assisted" and len(warnings) == 1


def test_non_mapping_yaml(tmp_path):
    _, warnings = load_repo_config(write(tmp_path, "- a\n- b\n"))
    assert len(warnings) == 1


def test_severity_gate():
    config = RepoPilotConfig(min_severity="high")
    assert config.severity_allows("critical") and config.severity_allows("high")
    assert not config.severity_allows("medium") and RepoPilotConfig().severity_allows("low")


def test_enforce_is_not_a_mode_but_the_rest_of_the_file_still_applies(tmp_path):
    config, warnings = load_repo_config(write(tmp_path, "mode: enforce\nmin_severity: high\n"))
    assert config.mode == "assisted" and config.min_severity == "high"
    assert len(warnings) == 1 and "enforce" in warnings[0]


def test_allowed_paths_and_owners_are_read_from_the_nested_layout(tmp_path):
    config, _ = load_repo_config(write(tmp_path, "owners: '@acme/platform'\nfixes:\n  allowed_paths: ['routes/**']\n"))
    assert config.allowed_paths == ["routes/**"] and config.owners == "@acme/platform"
