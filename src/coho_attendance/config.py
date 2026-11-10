"""运行配置：账号、密码、租户码全部从环境变量注入，不落盘、不写死。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://w.coho.net.cn"

#: 各配置项的变量名，按优先级从高到低。第一个是推荐写法，后面的用于兼容旧脚本。
_USERNAME_KEYS = ("COHO_USERNAME", "COHO_USER", "COHO_ACCOUNT")
_PASSWORD_KEYS = ("COHO_PASSWORD", "COHO_PASSWD", "COHO_PWD")
_TENANT_KEYS = ("COHO_TENANT_CODE", "COHO_TENANT")


class ConfigError(RuntimeError):
    """缺少或非法的环境变量配置。"""


def _first_env(keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return None


def _candidate_env_files(explicit: str | os.PathLike[str] | None) -> list[Path]:
    """按优先级列出可能存放 .env 的位置。

    MCP 客户端（如 DeepSeek Harness）会用任意工作目录拉起本进程，
    所以不能只依赖 cwd。这里从包所在目录逐级向上找项目根，
    命中含 pyproject.toml 或 .env 的目录即视为项目根。
    """
    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit))

    override = os.getenv("COHO_ENV_FILE")
    if override and override.strip():
        candidates.append(Path(override.strip()))

    candidates.append(Path.cwd() / ".env")

    here = Path(__file__).resolve().parent
    for directory in (here, *here.parents):
        if (directory / "pyproject.toml").is_file() or (directory / ".env").is_file():
            candidates.append(directory / ".env")
        if directory.name == "site-packages" or len(directory.parts) <= 1:
            break

    # 去重但保留顺序
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def load_env(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """加载第一个存在的 .env；真实环境变量优先，不会被覆盖。

    返回实际加载的文件路径，没找到则返回 None。
    """
    for path in _candidate_env_files(explicit):
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)
            return path
    return None


@dataclass(frozen=True)
class Settings:
    """一次会话使用的连接参数。"""

    username: str
    password: str = field(repr=False)
    tenant_code: str
    base_url: str = DEFAULT_BASE_URL

    @property
    def gateway(self) -> str:
        return f"{self.base_url.rstrip('/')}/gateway"

    @classmethod
    def from_env(cls, *, dotenv_path: str | os.PathLike[str] | None = None) -> "Settings":
        """从环境变量构造配置。

        先定位并加载 `.env`（已存在的真实环境变量优先，不会被覆盖）。
        查找范围覆盖 cwd、项目根以及 `COHO_ENV_FILE` 指定的路径，
        这样 MCP 客户端用任意工作目录拉起进程时也能读到凭据。
        账号、密码、租户码任一缺失时抛 ConfigError，MCP 工具会把这条信息原样带回给调用方，
        提示运维去补环境变量，而不是让请求带着空值打到服务端。
        """
        load_env(dotenv_path)

        username = _first_env(_USERNAME_KEYS)
        password = _first_env(_PASSWORD_KEYS)
        tenant_code = _first_env(_TENANT_KEYS)

        missing: list[str] = []
        if not username:
            missing.append("账号（" + " / ".join(_USERNAME_KEYS) + "）")
        if not password:
            missing.append("密码（" + " / ".join(_PASSWORD_KEYS) + "）")
        if not tenant_code:
            missing.append("租户码（" + " / ".join(_TENANT_KEYS) + "）")
        if missing:
            raise ConfigError(
                "缺少必需的登录配置：" + "、".join(missing) + "。"
                "账号、密码、租户码只从环境变量读取，不接受工具参数传入。"
            )

        return cls(
            username=username,
            password=password,
            tenant_code=tenant_code,
            base_url=(os.getenv("COHO_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
        )
