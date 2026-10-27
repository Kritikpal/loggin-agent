import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

ROOT_DIR = Path(__file__).resolve().parents[2]
NUMBERED_GROQ_KEY = re.compile(r"groq_api_key_(\d+)")


class Settings(BaseSettings):
    # extra="allow" so GROQ_API_KEY_1, _2, ... reach `model_extra`; any number of them can be added without code changes
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="allow")

    database_url: str
    test_database_url: str | None = None
    groq_api_key: str | None = None
    # GROQ_API_KEY, then GROQ_API_KEY_1, _2, ... in number order; blanks and repeats dropped. Filled below.
    groq_api_keys: list[str] = Field(default_factory=list)
    groq_model: str = "openai/gpt-oss-120b"  # must support tool calling; override with GROQ_MODEL
    groq_fast_model: str | None = None  # cheaper model for the router and approval check; unset = use groq_model

    @model_validator(mode="after")
    def _collect_groq_keys(self) -> "Settings":
        numbered = sorted(
            (int(m.group(1)), str(value))
            for name, value in (self.model_extra or {}).items()
            if (m := NUMBERED_GROQ_KEY.fullmatch(name.lower()))
        )
        keys = (k.strip() for k in [self.groq_api_key or "", *(v for _, v in numbered)])
        self.groq_api_keys = list(dict.fromkeys(k for k in keys if k))
        return self

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
