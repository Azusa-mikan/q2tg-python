"""从项目根目录的 .env 加载并严格校验应用配置。"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from src.log import baselog

ENV_PATH = Path(__file__).parents[1] / ".env"
DEFAULT_DATABASE_URL = (
    f"sqlite:///{(Path(__file__).parents[1] / 'data' / 'q2tg.db').as_posix()}"
)


class AppSettings(BaseSettings):
    """应用配置；字段名与 Q2TG_ 前缀的环境变量一一对应。"""

    model_config = SettingsConfigDict(
        env_file=ENV_PATH,
        env_file_encoding="utf-8",
        env_prefix="Q2TG_",
        extra="forbid",
        hide_input_in_errors=True,
    )

    app_port: int = Field(default=8000, ge=1025, le=65535)
    database_url: str = DEFAULT_DATABASE_URL
    onebot_token: str = Field(min_length=1)
    onebot_media_url: str = Field(min_length=1)
    onebot_proxy_url: str | None = None
    tgbot_token: str = Field(min_length=1)
    tgbot_admin: int
    tgbot_proxy_url: str | None = None

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError("Q2TG_DATABASE_URL 不是有效的数据库 URL") from None
        if url.drivername not in {"sqlite", "mysql", "postgresql"}:
            raise ValueError(
                "Q2TG_DATABASE_URL 仅支持 sqlite、mysql 或 postgresql"
            )
        if not url.database:
            raise ValueError("Q2TG_DATABASE_URL 必须包含数据库名或 SQLite 文件路径")
        return url.render_as_string(hide_password=False)

    @field_validator("onebot_media_url")
    @classmethod
    def validate_media_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("Q2TG_ONEBOT_MEDIA_URL 必须是 HTTP(S) URL")
        return value

    @field_validator("onebot_proxy_url", "tgbot_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.startswith(("http://", "https://", "socks5://", "socks5h://")):
            raise ValueError("代理必须是 HTTP(S) 或 SOCKS5 URL")
        return value


def load_settings(env_file: Path = ENV_PATH) -> AppSettings:
    """加载指定 .env；Callable 隔离 BaseSettings 的动态构造参数。"""
    settings_factory = cast(Callable[..., AppSettings], AppSettings)
    return settings_factory(_env_file=env_file)


def load_config() -> AppSettings:
    """加载项目 .env 与进程环境变量；环境变量优先于文件值。"""
    try:
        return load_settings()
    except (OSError, UnicodeError, ValidationError):
        baselog.exception("配置解析错误")
        sys.exit(1)


config = load_config()
