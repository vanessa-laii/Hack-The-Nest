# Settings for the backend, read once from environment variables and the repo-root .env.
# Inputs: env vars listed in docs/CONTRACTS.md §7 (secrets included; never print or log them).
# Outputs: `settings` singleton plus resolved paths (REPO_ROOT, CONTRACTS_DIR, settings.data_dir).
from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed anchor paths, computed from this file's location rather than the current
# working directory. Why: uvicorn, the validate script and spike code all run
# from different folders, and they must agree on where contracts/ and data/ are.
BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
CONTRACTS_DIR = REPO_ROOT / "contracts"


class Settings(BaseSettings):
    """Every backend setting in one typed object.

    What: pydantic-settings reads each field from the env var named in its
    `alias`, falling back to the repo-root .env file, then to the default.
    Why: one place to read config (CLAUDE.md: "read from env via app/config.py"),
    and type checking turns a typo in .env into a startup error rather than a
    confusing failure halfway through a story build.
    """

    # extra="ignore": the shared .env also holds Vanessa's web-only variables
    # (ELEVENLABS_*, NEXT_PUBLIC_*). The backend must skip them, not crash on them.
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- General / pipeline settings (CONTRACTS §7) ---
    data_dir_raw: str = Field("../data", alias="DATA_DIR")
    em_mock: bool = Field(False, alias="EM_MOCK")
    aoi_km: float = Field(6, alias="AOI_KM")
    pipeline_timeout_s: int = Field(180, alias="PIPELINE_TIMEOUT_S")  # HL-5
    web_origin: str = Field("http://localhost:3000", alias="WEB_ORIGIN")
    nominatim_user_agent: str = Field("earth-memory-hackthenest/1.0", alias="NOMINATIM_USER_AGENT")

    # --- Model + search providers ---
    # Secrets are SecretStr. Why: if a settings object is ever printed or logged
    # by accident, it shows '**********' instead of the key (CLAUDE.md #3).
    # Read the real value only at the call site with .get_secret_value().
    llm_provider: str = Field("gemini", alias="LLM_PROVIDER")
    llm_model: str = Field("gemini-3.5-flash-lite", alias="LLM_MODEL")
    openai_api_key: SecretStr | None = Field(None, alias="OPENAI_API_KEY")
    gemini_api_key: SecretStr | None = Field(None, alias="GEMINI_API_KEY")
    gemini_vision_model: str = Field("gemini-3.5-flash-lite", alias="GEMINI_VISION_MODEL")
    exa_api_key: SecretStr | None = Field(None, alias="EXA_API_KEY")
    mongodb_uri: SecretStr | None = Field(None, alias="MONGODB_URI")

    @field_validator("aoi_km")
    @classmethod
    def _aoi_in_bounds(cls, v: float) -> float:
        """Rejects an area outside 3–10 km. Why: hard limit HL-6. Larger areas
        blow the time budget; smaller ones don't show change at 30 m pixels."""
        if not 3 <= v <= 10:
            raise ValueError("AOI_KM must be between 3 and 10 (HL-6)")
        return v

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        """Allows only the two providers llm.py knows how to talk to. Why: a typo
        like 'opneai' should fail at startup, not on the first agent call."""
        v = v.strip().lower()
        if v not in {"gemini", "openai"}:
            raise ValueError("LLM_PROVIDER must be 'gemini' or 'openai'")
        return v

    @property
    def data_dir(self) -> Path:
        """The absolute DATA_DIR path.

        What: a relative DATA_DIR is resolved against backend/, so the shared
        .env value '../data' points to <repo>/data.
        Why: web/ uses the same '../data' relative to web/, so both apps land on
        the same folder on the demo laptop (CONTRACTS §6).
        """
        p = Path(self.data_dir_raw)
        return (p if p.is_absolute() else (BACKEND_DIR / p)).resolve()


# Created once at import time, so every module shares the same settings.
settings = Settings()
