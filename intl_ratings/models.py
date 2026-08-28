"""国际评级报表 pydantic 模型与字段常量。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

REPORT_COLUMNS = [
    "发行体",
    "穆迪评级",
    "标普评级",
    "惠誉评级",
    "债务人最近一期決算是否亏损(是/否)",
    "是否上市（是/否）",
    "若上市，债务人是否被上市废止(是/否)",
    "债券价格是否大幅下跌（月环比跌幅超过5%）等",
    "皆无评级的話请写明理由",
    "评级是否变化",
]

NEED_REVIEW = "[需人工复核]"
YES = "是"
NO = "否"
NR = "NR"
NOT_LISTED = "未上市"
NO_PUBLIC_TRADE = "无公开交易数据"

YnOrReview = Literal["是", "否", "[需人工复核]"]
ListedFlag = Literal["是", "否", "[需人工复核]"]
DelistFlag = Literal["是", "否", "未上市", "[需人工复核]"]
PriceDropFlag = Literal["是", "否", "无公开交易数据", "[需人工复核]"]


class EntityMapping(BaseModel):
    """实体映射层输出：发行体 → 母公司 / 代码。"""

    issuer_name: str
    parent_name: str = ""
    parent_aliases: list[str] = Field(default_factory=list)
    stock_ticker: str = ""
    inherit_ticker: str = ""
    bond_tickers: list[str] = Field(default_factory=list)
    isin: str = ""
    figi: str = ""
    # TradingView：tvDatafeed.get_hist(symbol, exchange)
    tv_symbol: str = ""
    tv_exchange: str = ""
    # 美股 SEC：sec-edgar-downloader
    us_ticker: str = ""
    cik: str = ""
    official_rating_url: str = ""
    is_offshore_spv: bool = False
    guarantor_name: str = ""
    mapping_source: str = "unknown"
    notes: str = ""

    @property
    def financial_entity_name(self) -> str:
        if self.is_offshore_spv:
            return self.guarantor_name or self.parent_name or self.issuer_name
        return self.issuer_name

    @property
    def financial_ticker(self) -> str:
        if self.is_offshore_spv and (self.inherit_ticker or "").strip():
            return self.inherit_ticker.strip()
        return (self.stock_ticker or "").strip()


class RatingSnapshot(BaseModel):
    moodys: str = NR
    sp: str = NR
    fitch: str = NR
    rating_changed: str = NO
    raw_source: str = ""
    source_urls: list[str] = Field(default_factory=list)

    @field_validator("moodys", "sp", "fitch", mode="before")
    @classmethod
    def normalize_rating(cls, v: object) -> str:
        if v is None:
            return NR
        s = str(v).strip()
        if not s or s in {"—", "-", "N/A", "n/a", "null"}:
            return NR
        return s

    def all_blank_or_nr(self) -> bool:
        vals = {self.moodys.upper(), self.sp.upper(), self.fitch.upper()}
        return vals <= {NR, ""}


class FinancialSnapshot(BaseModel):
    is_loss: str = NEED_REVIEW
    net_income: Optional[float] = None
    period_label: str = ""
    inherited_from: str = ""
    raw_source: str = ""


class ListingSnapshot(BaseModel):
    is_listed: str = NO
    is_delisted_or_st: str = NOT_LISTED
    ticker: str = ""
    status_label: str = ""
    raw_source: str = ""


class BondPriceSnapshot(BaseModel):
    price_drop_flag: str = NO_PUBLIC_TRADE
    change_pct: Optional[float] = None  # ((今-30日前)/30日前)*100，下跌为负
    max_drop_pct: Optional[float] = None  # 兼容旧字段：正数跌幅
    tickers_checked: list[str] = Field(default_factory=list)
    raw_source: str = ""


class IssuerRiskModel(BaseModel):
    """对外标准风险行模型（pydantic 校验）。"""

    发行体: str = Field(min_length=1)
    穆迪评级: str = NR
    标普评级: str = NR
    惠誉评级: str = NR
    债务人最近一期決算是否亏损: str = Field(
        default=NEED_REVIEW,
        alias="债务人最近一期決算是否亏损(是/否)",
    )
    是否上市: str = Field(default=NO, alias="是否上市（是/否）")
    上市废止: str = Field(
        default=NOT_LISTED,
        alias="若上市，债务人是否被上市废止(是/否)",
    )
    债券价格大幅下跌: str = Field(
        default=NO_PUBLIC_TRADE,
        alias="债券价格是否大幅下跌（月环比跌幅超过5%）等",
    )
    皆无评级理由: str = Field(default="", alias="皆无评级的話请写明理由")
    评级是否变化: str = NO
    # 非导出字段：页面“来源”按钮使用，仅保留三大评级机构官方链接。
    rating_source_urls: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    def to_excel_dict(self) -> dict[str, str]:
        data = self.model_dump(by_alias=True)
        return {col: str(data.get(col, "") or "") for col in REPORT_COLUMNS}


# 兼容旧名
IssuerReportRow = IssuerRiskModel


class IssuerAnalysisResult(BaseModel):
    mapping: EntityMapping
    ratings: RatingSnapshot = Field(default_factory=RatingSnapshot)
    financials: FinancialSnapshot = Field(default_factory=FinancialSnapshot)
    listing: ListingSnapshot = Field(default_factory=ListingSnapshot)
    bond_price: BondPriceSnapshot = Field(default_factory=BondPriceSnapshot)
    no_rating_reason: str = ""
    errors: list[str] = Field(default_factory=list)

    def to_report_row(self) -> IssuerRiskModel:
        return IssuerRiskModel.model_validate(
            {
                "发行体": self.mapping.issuer_name,
                "穆迪评级": self.ratings.moodys,
                "标普评级": self.ratings.sp,
                "惠誉评级": self.ratings.fitch,
                "债务人最近一期決算是否亏损(是/否)": self.financials.is_loss,
                "是否上市（是/否）": self.listing.is_listed,
                "若上市，债务人是否被上市废止(是/否)": self.listing.is_delisted_or_st,
                "债券价格是否大幅下跌（月环比跌幅超过5%）等": self.bond_price.price_drop_flag,
                "皆无评级的話请写明理由": self.no_rating_reason,
                "评级是否变化": self.ratings.rating_changed,
                "rating_source_urls": self.ratings.source_urls,
            }
        )
