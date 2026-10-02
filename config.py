"""配置模块。

这一段在干嘛：
  1. 从 `.env` 文件 + 系统环境变量里读配置（真实环境变量优先，不会被 .env 覆盖）；
  2. 打包成一个不可变的 `Config` 对象，其他模块统一 `Config.from_env()` 拿配置；
  3. 关键安全规则：只要缺 shop / token，或者显式 `MOCK=true`，就进入 **mock 模式**，
     后面所有网络请求都会被换成内置假数据，保证不会误连真实店铺；
  4. 提供 `setup_logging()`，把日志同时写到控制台和 `logs/xxx.log`。

安全说明：密钥只从环境变量读，绝不硬编码，也绝不写进日志。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: 项目根目录（本文件所在目录）
BASE_DIR: Final[Path] = Path(__file__).resolve().parent
#: 日志目录
LOG_DIR: Final[Path] = BASE_DIR / "logs"
#: 默认数据库位置
DEFAULT_DB_PATH: Final[Path] = BASE_DIR / "data" / "shopify.db"
#: 默认 .env 位置
ENV_FILE: Final[Path] = BASE_DIR / ".env"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on"})


def load_env_file(path: Path = ENV_FILE) -> None:
    """把 .env 文本读进 os.environ。

    这里手写了一个极简解析器（避免多装一个 python-dotenv 依赖）：
      - 忽略空行和以 # 开头的注释行
      - 支持 `KEY=VALUE`，会去掉值两边的引号
      - 用 setdefault：已经存在的真实环境变量优先，不会被文件覆盖
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _get(name: str, default: str = "") -> str:
    """读一个字符串环境变量，空白当没填。"""
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _get_flag(name: str, default: bool = False) -> bool:
    """读一个布尔开关：true/1/yes/y/on 算真。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def setup_logging(name: str = "app", level: int = logging.INFO) -> logging.Logger:
    """建一个同时输出到控制台 + `logs/<name>.log` 的 logger。

    其他模块只要 `log = setup_logging(__name__)` 就能用。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if logger.handlers:  # 防止重复添加 handler（重复会打印两遍）
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_DIR / f"{name.replace('.', '_')}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger


@dataclass(frozen=True, slots=True)
class Config:
    """一次性装齐所有运行参数。"""

    shop: str
    access_token: str
    api_version: str
    webhook_secret: str
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    mock: bool
    db_path: Path

    # ---------- 派生属性 ----------

    @property
    def is_mock(self) -> bool:
        """是否走 mock：显式开了 MOCK，或者压根没配好 Shopify 凭据。"""
        return self.mock or not self.shop or not self.access_token

    @property
    def has_llm(self) -> bool:
        """有没有配 DeepSeek key；没有就用内置英文模板兜底。"""
        return bool(self.deepseek_api_key)

    @property
    def api_base_url(self) -> str:
        """Admin REST API 的根地址。"""
        return f"https://{self.shop}/admin/api/{self.api_version}"

    def describe(self) -> str:
        """给日志/CLI 看的一行配置摘要（**不含任何密钥**）。"""
        mode = "MOCK(内置假数据)" if self.is_mock else f"LIVE({self.shop})"
        llm = f"DeepSeek:{self.deepseek_model}" if self.has_llm else "内置英文模板(兜底)"
        return f"mode={mode} | email_engine={llm} | db={self.db_path}"

    # ---------- 构造 ----------

    @classmethod
    def from_env(cls, *, force_mock: bool = False) -> "Config":
        """从环境变量构造配置；`force_mock=True` 时无条件进 mock（CLI 的 --mock）。"""
        load_env_file()
        db_raw = _get("DB_PATH")
        return cls(
            shop=_get("SHOPIFY_SHOP"),
            access_token=_get("SHOPIFY_ACCESS_TOKEN"),
            api_version=_get("SHOPIFY_API_VERSION", "2024-07"),
            webhook_secret=_get("SHOPIFY_WEBHOOK_SECRET"),
            deepseek_api_key=_get("DEEPSEEK_API_KEY"),
            deepseek_base_url=_get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=_get("DEEPSEEK_MODEL", "deepseek-chat"),
            mock=_get_flag("MOCK", default=False) or force_mock,
            db_path=Path(db_raw) if db_raw else DEFAULT_DB_PATH,
        )
