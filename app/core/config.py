import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
NUMBERED_GROQ_KEY = re.compile(r"groq_api_key_(\d+)")


class Settings(BaseSettings):
    # extra="allow" so GROQ_API_KEY_1, _2, ... reach `model_extra`; any number of them can be added without code changes
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="allow")

    database_url: str
    groq_api_key: str | None = None
    # GROQ_API_KEY, then GROQ_API_KEY_1, _2, ... in number order; blanks and repeats dropped. Filled below.
    groq_api_keys: list[str] = Field(default_factory=list)
    sources_config_path: Path = ROOT_DIR / "sources.yaml"  # override with SOURCES_CONFIG_PATH
    groq_model: str = "openai/gpt-oss-120b"  # must support tool calling; override with GROQ_MODEL
    # a stable place for the downloaded model (the OS temp dir can be cleaned); override with EMBEDDING_CACHE_DIR
    embedding_cache_dir: Path = ROOT_DIR / ".cache" / "fastembed"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
