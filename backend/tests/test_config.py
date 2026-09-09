import pytest

from deepcontrib.config import ConfigError, Settings, load_settings


def test_openai_settings_fail_with_actionable_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPCONTRIB_MODEL", "openai:gpt-5.5")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = load_settings()

    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        settings.validate_model_credentials()


def test_load_settings_uses_explicit_data_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPCONTRIB_MODEL", "ollama:llama3.2")
    monkeypatch.setenv("DEEPCONTRIB_DATA_DIR", "./tmp/deepcontrib")

    settings = load_settings()

    assert settings.model == "ollama:llama3.2"
    assert settings.data_dir.name == "deepcontrib"


def test_load_settings_reads_optional_pinned_test_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DEEPCONTRIB_TEST_IMAGE",
        "python:3.11-slim@sha256:" + "a" * 64,
    )

    settings = load_settings()

    assert settings.test_image == "python:3.11-slim@sha256:" + "a" * 64


def test_settings_reject_invalid_model_identifier() -> None:
    with pytest.raises(ConfigError, match="provider:model"):
        Settings(model="gpt-5.5").validate_model_credentials()


def test_settings_reject_invalid_database_url() -> None:
    with pytest.raises(ConfigError, match="PostgreSQL URL"):
        Settings(database_url="sqlite:///tmp.db").validate_database_url()


def test_settings_accept_openai_key_from_explicit_mapping() -> None:
    settings = Settings(model="openai:gpt-5.5")

    settings.validate_model_credentials({"OPENAI_API_KEY": "test-only"})


def test_load_settings_reads_and_validates_runtime_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPCONTRIB_MAX_TASK_SECONDS", "120")
    monkeypatch.setenv("DEEPCONTRIB_MAX_MODEL_CALLS", "5")

    settings = load_settings()

    assert settings.max_task_seconds == 120
    assert settings.max_model_calls == 5


def test_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ConfigError, match="positive"):
        Settings(max_task_seconds=0).validate_limits()
