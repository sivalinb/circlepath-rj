from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    data_dir: Path = Field(default=Path("data"), validation_alias="CIRCLEPATH_DATA_DIR")
    host: str = Field(default="127.0.0.1", validation_alias="CIRCLEPATH_HOST")
    port: int = Field(default=7860, validation_alias="CIRCLEPATH_PORT")
    ai_provider: str = Field(default="gemini", validation_alias="CIRCLEPATH_AI_PROVIDER")
    gemini_model: str = Field(default="gemini-3.1-flash-lite", validation_alias="CIRCLEPATH_GEMINI_MODEL")
    gemini_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="GEMINI_API_KEY")
    gemini_api_key_file: str = Field(default="", validation_alias="GEMINI_API_KEY_FILE")
    arize_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="ARIZE_API_KEY")
    arize_api_key_file: str = Field(default="", validation_alias="ARIZE_API_KEY_FILE")
    arize_space_id: str = Field(default="", validation_alias="ARIZE_SPACE_ID")
    arize_project: str = Field(default="circlepath-rj", validation_alias="ARIZE_PROJECT_NAME")
    arize_endpoint: str = Field(
        default="https://otlp.arize.com/v1/traces", validation_alias="ARIZE_ENDPOINT"
    )
    otlp_endpoint: str = Field(default="", validation_alias="OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    grafana_url: str = Field(
        default="http://127.0.0.1:3300/d/circlepath/circlepath",
        validation_alias="CIRCLEPATH_GRAFANA_URL",
    )
    google_oauth_client_file: str = Field(default="", validation_alias="GOOGLE_OAUTH_CLIENT_FILE")
    smtp_enabled: bool = Field(default=False, validation_alias="CIRCLEPATH_SMTP_ENABLED")
    smtp_host: str = Field(default="", validation_alias="CIRCLEPATH_SMTP_HOST")
    smtp_port: int = Field(default=587, validation_alias="CIRCLEPATH_SMTP_PORT")
    smtp_username: str = Field(default="", validation_alias="CIRCLEPATH_SMTP_USERNAME")
    smtp_password: SecretStr = Field(default=SecretStr(""), validation_alias="CIRCLEPATH_SMTP_PASSWORD")
    smtp_from: str = Field(default="", validation_alias="CIRCLEPATH_SMTP_FROM")

    @staticmethod
    def _key(value: SecretStr, filename: str) -> str:
        if value.get_secret_value():
            return value.get_secret_value()
        if filename:
            return Path(filename).expanduser().read_text().strip()
        return ""

    def gemini_key(self) -> str:
        return self._key(self.gemini_api_key, self.gemini_api_key_file)

    def arize_key(self) -> str:
        return self._key(self.arize_api_key, self.arize_api_key_file)

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "circlepath.db"
