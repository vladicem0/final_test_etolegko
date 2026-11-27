from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )
    database_url: str = "sqlite:///./keitaro.sqlite3"


class Settings(DatabaseSettings):
    keitaro_base_url: AnyHttpUrl = AnyHttpUrl(
        "https://keitaro-for-tests.etolegko.com/admin_api/v1"
    )
    keitaro_api_key: SecretStr = Field(min_length=1)
    keitaro_domain_id: int = Field(default=1813, gt=0)
    keitaro_group_id: int = Field(default=10332, gt=0)
    keitaro_traffic_source_id: int = Field(default=836, gt=0)

    def campaign_admin_url(self, campaign_id: int) -> str:
        host = str(self.keitaro_base_url).split("/admin_api/", 1)[0].rstrip("/")
        return f"{host}/admin/#!/campaign.editor/{campaign_id}"
