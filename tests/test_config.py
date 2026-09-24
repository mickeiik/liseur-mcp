from __future__ import annotations

from liseur_mcp.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "LISEUR_URL": "http://liseur.test",
        "LISEUR_TOKEN": "token",
    }
    values.update(overrides)
    return Settings(**values)


def test_allowed_origins_default_is_empty() -> None:
    assert _settings().allowed_origins == []


def test_allowed_origins_parses_csv_dropping_blanks_and_spaces() -> None:
    settings = _settings(MCP_ALLOWED_ORIGINS=" http://a.test ,http://b.test, ,")
    assert settings.allowed_origins == ["http://a.test", "http://b.test"]
