# Settings for the backend, read once from environment variables and the repo-root .env.
# Inputs: env vars listed in docs/CONTRACTS.md §7 (secrets included; never print or log them).
# Outputs: `settings` singleton plus resolved paths (REPO_ROOT, CONTRACTS_DIR, settings.data_dir).
#
# How it works:
#   1. Work out the fixed folders (backend/, repo root, contracts/) from this file's location.
#   2. Settings reads each field from its env var, then from the repo-root .env, then the default.
#   3. Validators reject bad values (area size, provider name) at startup.
#   4. One shared `settings` object is created on import.
from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Paths come from this file's location, not the current folder, because uvicorn,
# scripts and spikes run from different folders but must find the same data.
BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
CONTRACTS_DIR = REPO_ROOT / "contracts"


class Settings(BaseSettings):
    """Every backend setting in one typed object, read from env vars or .env.
    Typed fields turn a typo in .env into a startup error, not a failure mid-build."""

    # The shared .env also holds web-only variables (ELEVENLABS_*, NEXT_PUBLIC_*).
    # extra="ignore" skips them instead of crashing.
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General / pipeline settings (CONTRACTS §7) ---
    data_dir_raw: str = Field("../data", alias="DATA_DIR")
    em_mock: bool = Field(False, alias="EM_MOCK")
    aoi_km: float = Field(6, alias="AOI_KM")
    pipeline_timeout_s: int = Field(180, alias="PIPELINE_TIMEOUT_S")  # HL-5
    web_origin: str = Field("http://localhost:3000", alias="WEB_ORIGIN")
    nominatim_user_agent: str = Field(
        "revisit-hackthenest/1.0", alias="NOMINATIM_USER_AGENT"
    )

    # --- Model + search providers ---
    # Keys are SecretStr, so printing settings shows '**********' (CLAUDE.md #3).
    # Read the real value only where it is used, with .get_secret_value().
    llm_provider: str = Field("gemini", alias="LLM_PROVIDER")
    llm_model: str = Field("gemini-3.5-flash-lite", alias="LLM_MODEL")
    openai_api_key: SecretStr | None = Field(None, alias="OPENAI_API_KEY")
    gemini_api_key: SecretStr | None = Field(None, alias="GEMINI_API_KEY")
    gemini_vision_model: str = Field("gemini-3.5-flash-lite", alias="GEMINI_VISION_MODEL")
    exa_api_key: SecretStr | None = Field(None, alias="EXA_API_KEY")
    # 1 = Exa answers only from the disk cache and never spends money (spikes, tests).
    exa_cached_only: bool = Field(False, alias="EXA_CACHED_ONLY")
    mongodb_uri: SecretStr | None = Field(None, alias="MONGODB_URI")

    @field_validator("aoi_km")
    @classmethod
    def _aoi_in_bounds(cls, v: float) -> float:
        """Rejects an area outside 3–10 km (HL-6).
        Bigger areas blow the time budget; smaller ones show no change at 30 m pixels."""
        if not 3 <= v <= 10:
            raise ValueError("AOI_KM must be between 3 and 10 (HL-6)")
        return v

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        """Allows only the two providers llm.py supports.
        A typo like 'opneai' then fails at startup, not on the first model call."""
        v = v.strip().lower()
        if v not in {"gemini", "openai"}:
            raise ValueError("LLM_PROVIDER must be 'gemini' or 'openai'")
        return v

    @property
    def data_dir(self) -> Path:
        """The absolute DATA_DIR. A relative value is taken from backend/, so '../data'
        means <repo>/data, the same folder web/ uses (CONTRACTS §6)."""
        path = Path(self.data_dir_raw)
        if not path.is_absolute():
            path = BACKEND_DIR / path
        return path.resolve()


# Created once at import time, so every module shares the same settings.
settings = Settings()
