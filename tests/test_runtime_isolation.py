from conftest import isolated_runtime_homes

from agentroute.config import agentroute_home, config_path, data_dir, default_config, save_config


def test_runtime_fixture_clears_inherited_absolute_paths(tmp_path, monkeypatch):
    outside_config = tmp_path / "outside.yaml"
    outside_config.write_text("preserve this config\n")
    outside_data = tmp_path / "outside-data"
    monkeypatch.setenv("AGENTROUTE_CONFIG", str(outside_config))
    monkeypatch.setenv("AGENTROUTE_DATA_DIR", str(outside_data))

    with monkeypatch.context() as isolated:
        fixture = isolated_runtime_homes.__wrapped__(isolated)
        next(fixture)
        try:
            assert config_path() == agentroute_home() / "config.yaml"
            assert data_dir() == agentroute_home()
            assert save_config(default_config()) != outside_config
            assert outside_config.read_text() == "preserve this config\n"
            assert not outside_data.exists()
        finally:
            fixture.close()
