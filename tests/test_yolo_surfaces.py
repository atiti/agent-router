"""YOLO user surfaces: help, CLI validation, doctor, banner and docs."""

from pathlib import Path

from test_hook import invoke
from typer.testing import CliRunner

from agentroute.audit import AuditStore
from agentroute.cli import app
from agentroute.config import default_config, load_config, save_config
from agentroute.doctor import run_doctor

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def test_help_lists_yolo_and_its_admission(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    result = runner.invoke(app, ["help"])
    assert "@yolo (blocked until policy.max_tier: yolo" in result.output
    config = default_config()
    config.policy.max_tier = "yolo"
    save_config(config, path)
    assert "@yolo (admitted" in runner.invoke(app, ["help"]).output


def test_backend_route_rejects_yolo_and_backend_add_reserves_it(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(path))
    save_config(default_config(), path)
    result = runner.invoke(app, ["backend-route", "yolo", "gpt"])
    assert result.exit_code != 0
    assert "yolo" not in load_config(path).routing.backend_by_tier
    result = runner.invoke(
        app, ["backend-add", "yolo", "--base-url", "http://127.0.0.1:9/v1", "--model", "m"]
    )
    assert result.exit_code != 0


def test_doctor_reports_yolo_admission():
    checks = {check.name: check for check in run_doctor(default_config())}
    assert checks["yolo"].status == "pass"
    assert "not admitted" in checks["yolo"].detail
    config = default_config()
    config.policy.max_tier = "yolo"
    detail = {check.name: check for check in run_doctor(config)}["yolo"].detail
    assert "gpt=gpt-6.2-astra/ultra" in detail
    assert "azure: not ready" in detail


def test_route_banner_labels_yolo_and_model_request(tmp_path):
    config = default_config()
    config.enabled = True
    config.policy.max_tier = "yolo"
    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@yolo design it")
    specific = output["hookSpecificOutput"]
    assert "◆ MODEL ROUTE · YOLO → gpt-6.2-astra · ultra reasoning" in specific["routeMessage"]
    assert "YOLO OPT-IN (explicit_tag)" in specific["routeMessage"]
    assert "MODEL_REQUEST: <FAST|NORMAL|SMART|MAX|YOLO>" in specific["additionalContext"]


def test_model_request_hides_yolo_until_admitted(tmp_path):
    config = default_config()
    config.enabled = True
    output = invoke(config, AuditStore(tmp_path / "audit.db"), "hello")
    assert "MODEL_REQUEST: <FAST|NORMAL|SMART|MAX>" in output["hookSpecificOutput"][
        "additionalContext"
    ]


def test_blocked_yolo_banner_says_not_run(tmp_path):
    config = default_config()
    config.enabled = True
    output = invoke(config, AuditStore(tmp_path / "audit.db"), "@yolo design it")
    assert "YOLO NOT RUN" in output["hookSpecificOutput"]["routeMessage"]
    assert output["systemMessage"].startswith("AgentRoute did not run this @yolo turn.")


def test_docs_describe_implemented_behavior():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "yolo-tier.md").read_text(encoding="utf-8")
    assert "### YOLO tier" in readme
    assert "max_tier: yolo" in readme
    assert "Status: implemented" in doc
    assert "| Live provider smoke |" in doc
