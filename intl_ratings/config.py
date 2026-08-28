"""加载 intl_ratings 配置。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "intl_ratings.yaml"


class IntlRatingsEnv(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepseek_api_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    openfigi_api_key: str = ""
    # TradingView（tvDatafeed，可选登录）
    tradingview_username: str = ""
    tradingview_password: str = ""
    # SEC EDGAR fair access：公司名 + 邮箱
    sec_edgar_company: str = "RiskIntelSystem"
    sec_edgar_email: str = "risk-intel@example.com"
    intl_ratings_config_path: str = str(DEFAULT_CONFIG_PATH)


class PathsConfig(BaseModel):
    input_dir: str = "data/intl_ratings/input"
    output_dir: str = "data/intl_ratings/output"
    raw_response_dir: str = "logs/raw_responses"
    error_log: str = "logs/error.log"
    entity_cache: str = "data/intl_ratings/entity_map_cache.json"
    issuers_json: str = "config/issuers.json"
    sec_edgar_dir: str = "data/intl_ratings/sec_edgar"


class PlaceholdersConfig(BaseModel):
    need_review: str = "[需人工复核]"
    no_public_trade: str = "无公开交易数据"
    not_listed: str = "未上市"
    nr: str = "NR"


class BondPriceConfig(BaseModel):
    lookback_days: int = 30
    drop_threshold_pct: float = 5.0  # 跌幅超过 5% ⇒ change_pct <= -5


class RatingChangeConfig(BaseModel):
    lookback_days: int = 90


class StaticEntityMap(BaseModel):
    parent_name: str = ""
    parent_aliases: list[str] = Field(default_factory=list)
    stock_ticker: str = ""
    bond_tickers: list[str] = Field(default_factory=list)
    is_offshore_spv: bool = False


class EntityMapperConfig(BaseModel):
    use_llm: bool = True
    temperature: float = 0.1
    static_map: dict[str, StaticEntityMap] = Field(default_factory=dict)


class SourcesConfig(BaseModel):
    moodys_api: bool = False
    sp_api: bool = False
    fitch_api: bool = False
    akshare: bool = True
    yfinance: bool = True
    openfigi: bool = True
    tvdatafeed: bool = True
    sec_edgar: bool = True
    playwright_ratings: bool = True
    bond_price_public: bool = True
    rating_change_feed: bool = False
    # 免费模式：通过秘塔检索三大机构的公开页面；只接纳官方域名结果。
    official_public_ratings: bool = True
    official_public_rating_max_results: int = 8
    official_public_rating_max_queries_per_run: int = 20


class PlaywrightRatingsConfig(BaseModel):
    headless: bool = True
    timeout_ms: int = 45000
    enable_agency_pages: bool = True


class RuntimeConfig(BaseModel):
    request_timeout_seconds: int = 60
    max_issuers: int = 0
    sleep_between_issuers: float = 0.4
    market_only: bool = False


class IntlRatingsConfig(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    input_files: list[str] = Field(
        default_factory=lambda: ["issuer_list.csv", "issuer_list.docx"]
    )
    placeholders: PlaceholdersConfig = Field(default_factory=PlaceholdersConfig)
    bond_price: BondPriceConfig = Field(default_factory=BondPriceConfig)
    rating_change: RatingChangeConfig = Field(default_factory=RatingChangeConfig)
    entity_mapper: EntityMapperConfig = Field(default_factory=EntityMapperConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    playwright_ratings: PlaywrightRatingsConfig = Field(
        default_factory=PlaywrightRatingsConfig
    )
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else ROOT / p


def load_yaml(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or Path(IntlRatingsEnv().intl_ratings_config_path)
    if not cfg_path.is_file():
        return {}
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


@lru_cache
def get_intl_config(config_path: str | None = None) -> IntlRatingsConfig:
    path = Path(config_path) if config_path else None
    raw = load_yaml(path)
    return IntlRatingsConfig.model_validate(raw)


def get_env() -> IntlRatingsEnv:
    return IntlRatingsEnv()
