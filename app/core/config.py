from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    test_database_url: str | None = None
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"  # must support tool calling; override with GROQ_MODEL

    @property
    def resolved_test_database_url(self) -> str:
        """Explicit TEST_DATABASE_URL, else the app database name with a `_test` suffix."""
        if self.test_database_url:
            return self.test_database_url
        url = make_url(self.database_url)
        return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
