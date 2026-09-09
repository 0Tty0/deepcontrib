"""Runtime configuration and environment validation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when a required runtime setting is invalid or missing."""


_API_KEY_BY_PROVIDER = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "baseten": "BASETEN_API_KEY",
}


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the local task service."""

    model: str = "openai:gpt-5.5"
    database_url: str = (
        "postgresql://deepcontrib:deepcontrib@localhost:5432/deepcontrib"
    )
    data_dir: Path = Path("data")
    test_image: str | None = None
    max_task_seconds: int = 1_800
    max_model_calls: int = 40

    @property
    def provider(self) -> str:
        """Return the provider portion of a ``provider:model`` identifier."""
        if ":" not in self.model:
            raise ConfigError(
                "DEEPCONTRIB_MODEL must use the provider:model format, "
                f"got {self.model!r}"
            )
        provider, model_name = self.model.split(":", 1)
        if not provider or not model_name:
            raise ConfigError(
                "DEEPCONTRIB_MODEL must use the provider:model format, "
                f"got {self.model!r}"
            )
        return provider

    def validate_model_credentials(
        self, environ: Mapping[str, str] | None = None
    ) -> None:
        """Fail early with the exact environment variable the model needs."""
        provider = self.provider
        key_name = _API_KEY_BY_PROVIDER.get(provider)
        if key_name is None:
            # Local providers such as Ollama do not use a provider API key.
            return
        environment = os.environ if environ is None else environ
        if not environment.get(key_name, "").strip():
            raise ConfigError(
                f"{key_name} is required for model provider {provider!r}; "
                "set it in the process environment"
            )

    def validate_database_url(self) -> None:
        """Validate the URL shape before attempting a database connection."""
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ConfigError(
                "DATABASE_URL must be a PostgreSQL URL beginning with postgresql://"
            )

    def validate_limits(self) -> None:
        if self.max_task_seconds <= 0:
            raise ConfigError("DEEPCONTRIB_MAX_TASK_SECONDS must be positive")
        if self.max_model_calls <= 0:
            raise ConfigError("DEEPCONTRIB_MAX_MODEL_CALLS must be positive")


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load settings without reading files or logging sensitive values."""
    environment = os.environ if environ is None else environ
    try:
        max_task_seconds = int(environment.get("DEEPCONTRIB_MAX_TASK_SECONDS", "1800"))
        max_model_calls = int(environment.get("DEEPCONTRIB_MAX_MODEL_CALLS", "40"))
    except ValueError as exc:
        raise ConfigError("task and model call limits must be integers") from exc
    settings = Settings(
        model=environment.get("DEEPCONTRIB_MODEL", "openai:gpt-5.5").strip(),
        database_url=environment.get(
            "DATABASE_URL",
            "postgresql://deepcontrib:deepcontrib@localhost:5432/deepcontrib",
        ).strip(),
        data_dir=Path(environment.get("DEEPCONTRIB_DATA_DIR", "data")),
        test_image=(environment.get("DEEPCONTRIB_TEST_IMAGE", "").strip() or None),
        max_task_seconds=max_task_seconds,
        max_model_calls=max_model_calls,
    )
    settings.validate_limits()
    return settings
