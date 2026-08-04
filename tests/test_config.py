import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.config import load_settings


class TestConfig:
    def _env(self, directory: str, *, proxy: str = "") -> Path:
        path = Path(directory) / ".env"
        path.write_text(
            "\n".join(
                (
                    "Q2TG_APP_PORT=8000",
                    "Q2TG_ONEBOT_TOKEN=onebot-token",
                    "Q2TG_ONEBOT_MEDIA_URL=http://127.0.0.1:8000/",
                    "Q2TG_ONEBOT_PROXY_URL=",
                    "Q2TG_TGBOT_TOKEN=telegram-token",
                    "Q2TG_TGBOT_ADMIN=123",
                    f"Q2TG_TGBOT_PROXY_URL={proxy}",
                )
            ),
            encoding="utf-8",
        )
        return path

    def test_env_file_is_loaded_and_normalized(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            settings = load_settings(self._env(directory))

        assert settings.app_port == 8000
        assert settings.onebot_media_url == "http://127.0.0.1:8000"
        assert settings.database_url.startswith("sqlite:///")
        assert settings.onebot_proxy_url is None
        assert settings.tgbot_proxy_url is None

    def test_process_environment_overrides_env_file(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"Q2TG_APP_PORT": "9000"}, clear=True),
        ):
            settings = load_settings(
                self._env(directory, proxy="socks5://127.0.0.1:1080")
            )

        assert settings.app_port == 9000
        assert settings.tgbot_proxy_url == "socks5://127.0.0.1:1080"

    def test_invalid_proxy_scheme_is_rejected(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(ValidationError),
        ):
            load_settings(self._env(directory, proxy="ftp://127.0.0.1"))

    def test_system_environment_works_without_env_file(self) -> None:
        environment = {
            "Q2TG_APP_PORT": "8080",
            "Q2TG_ONEBOT_TOKEN": "onebot-token",
            "Q2TG_ONEBOT_MEDIA_URL": "https://bridge.example",
            "Q2TG_ONEBOT_PROXY_URL": "http://proxy.example:8080",
            "Q2TG_TGBOT_TOKEN": "telegram-token",
            "Q2TG_TGBOT_ADMIN": "456",
            "Q2TG_TGBOT_PROXY_URL": "socks5h://proxy.example:1080",
        }
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            environment,
            clear=True,
        ):
            settings = load_settings(Path(directory) / "missing.env")

        assert settings.app_port == 8080
        assert settings.tgbot_admin == 456
        assert settings.onebot_proxy_url == "http://proxy.example:8080"
        assert settings.tgbot_proxy_url == "socks5h://proxy.example:1080"

    def test_app_port_defaults_to_8000(self) -> None:
        environment = {
            "Q2TG_ONEBOT_TOKEN": "onebot-token",
            "Q2TG_ONEBOT_MEDIA_URL": "https://bridge.example",
            "Q2TG_TGBOT_TOKEN": "telegram-token",
            "Q2TG_TGBOT_ADMIN": "456",
        }
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            environment,
            clear=True,
        ):
            settings = load_settings(Path(directory) / "missing.env")

        assert settings.app_port == 8000

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite:////tmp/q2tg.db",
            "mysql://user:password@localhost:3306/q2tg",
            "postgresql://user:password@localhost:5432/q2tg",
        ),
    )
    def test_supported_database_urls_are_accepted(self, database_url: str) -> None:
        environment = {
            "Q2TG_ONEBOT_TOKEN": "onebot-token",
            "Q2TG_ONEBOT_MEDIA_URL": "https://bridge.example",
            "Q2TG_TGBOT_TOKEN": "telegram-token",
            "Q2TG_TGBOT_ADMIN": "456",
        }
        with TemporaryDirectory() as directory:
            environment["Q2TG_DATABASE_URL"] = database_url
            with patch.dict(os.environ, environment, clear=True):
                settings = load_settings(Path(directory) / "missing.env")
            assert settings.database_url == database_url

    def test_database_url_with_driver_name_is_rejected(self) -> None:
        environment = {
            "Q2TG_DATABASE_URL": "postgresql+asyncpg://user:password@localhost/q2tg",
            "Q2TG_ONEBOT_TOKEN": "onebot-token",
            "Q2TG_ONEBOT_MEDIA_URL": "https://bridge.example",
            "Q2TG_TGBOT_TOKEN": "telegram-token",
            "Q2TG_TGBOT_ADMIN": "456",
        }
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, environment, clear=True),
            pytest.raises(ValidationError),
        ):
            load_settings(Path(directory) / "missing.env")
