import json

import futu_quant.config as config


def test_volatility_campaigns_reload_from_file_not_stale_environment(
    tmp_path, monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    stale = [{"code": "US.OLD", "enabled": True}]
    current = [{"code": "US.NEW", "enabled": True}]
    env_file.write_text(
        f"VOLATILITY_CAMPAIGNS={json.dumps(current, separators=(',', ':'))}\n"
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("VOLATILITY_CAMPAIGNS", json.dumps(stale))

    assert config.load_volatility_campaigns() == current


def test_gamma_campaigns_reload_after_file_change(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    env_file.write_text('GAMMA_CAMPAIGNS=[{"code":"US.FIRST"}]\n')
    assert config.load_gamma_campaigns() == [{"code": "US.FIRST"}]

    env_file.write_text('GAMMA_CAMPAIGNS=[]\n')
    assert config.load_gamma_campaigns() == []
