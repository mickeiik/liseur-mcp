from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(path: Path, alias: str) -> str:
    try:
        return path.read_text(encoding="utf-8").removesuffix("\n")
    except OSError as exc:
        raise ValueError(f"unable to read {alias}") from exc


class Settings(BaseSettings):
    """Validated process configuration, read from the environment."""

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        extra="forbid",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    liseur_url: str = Field(..., alias="LISEUR_URL")
    request_timeout: float = Field(30.0, gt=0, le=300, alias="LISEUR_TIMEOUT_SECONDS")
    token_input: SecretStr | None = Field(None, alias="LISEUR_TOKEN")
    token_file: Path | None = Field(None, alias="LISEUR_TOKEN_FILE")
    token: SecretStr = Field(default=SecretStr(""), exclude=True)

    transport: Literal["stdio", "streamable-http"] = Field("stdio", alias="MCP_TRANSPORT")
    host: str = Field("127.0.0.1", alias="MCP_HOST")
    port: int = Field(8000, ge=1, le=65535, alias="MCP_PORT")
    mcp_path: str = Field("/mcp", alias="MCP_PATH")
    auth_token_input: SecretStr | None = Field(None, alias="MCP_AUTH_TOKEN")
    auth_token_file: Path | None = Field(None, alias="MCP_AUTH_TOKEN_FILE")
    auth_token: SecretStr = Field(default=SecretStr(""), exclude=True)
    allowed_hosts_csv: str = Field(
        "localhost,localhost:*,127.0.0.1,127.0.0.1:*", alias="MCP_ALLOWED_HOSTS"
    )
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @field_validator("liseur_url")
    @classmethod
    def valid_liseur_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LISEUR_URL must be an HTTP(S) base URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError(
                "LISEUR_URL must not contain credentials, a query or a fragment"
            )
        return value

    @field_validator("mcp_path")
    @classmethod
    def valid_mcp_path(cls, value: str) -> str:
        if not value.startswith("/") or value == "/":
            raise ValueError("MCP_PATH must be an absolute, non-root path")
        if any(character in value for character in "{}?#") or any(
            character.isspace() for character in value
        ):
            raise ValueError("MCP_PATH must be a static URL path without templates or whitespace")
        return value.rstrip("/")

    @property
    def allowed_hosts(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts_csv.split(",") if item.strip()]

    @model_validator(mode="before")
    @classmethod
    def resolve_secret_files(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)

        def resolve(
            direct_alias: str,
            direct_field: str,
            file_alias: str,
            file_field: str,
            required: bool,
        ) -> SecretStr | None:
            direct = values.get(direct_alias, values.get(direct_field))
            file_value = values.get(file_alias, values.get(file_field))
            if direct is None and file_value is None:
                if required:
                    raise ValueError(f"{direct_alias} or {file_alias} is required")
                return None
            if direct is not None and file_value is not None:
                raise ValueError(f"set only one of {direct_alias} and {file_alias}")
            if file_value is not None:
                raw = _read_secret(Path(str(file_value)), file_alias)
            else:
                raw = direct.get_secret_value() if isinstance(direct, SecretStr) else str(direct)
            if not raw:
                raise ValueError(f"{direct_alias} must not be empty")
            return SecretStr(raw)

        values["token"] = resolve(
            "LISEUR_TOKEN", "token_input", "LISEUR_TOKEN_FILE", "token_file", True
        )
        auth_token = resolve(
            "MCP_AUTH_TOKEN", "auth_token_input", "MCP_AUTH_TOKEN_FILE", "auth_token_file", False
        )
        if auth_token is not None:
            values["auth_token"] = auth_token
        return values

    @model_validator(mode="after")
    def require_http_auth(self) -> Self:
        if self.transport == "streamable-http" and not self.auth_token.get_secret_value():
            raise ValueError(
                "MCP_AUTH_TOKEN or MCP_AUTH_TOKEN_FILE is required "
                "for the streamable-http transport"
            )
        return self
