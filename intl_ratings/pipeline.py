"""国际评级分析流水线编排。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from intl_ratings.config import IntlRatingsConfig, get_env, get_intl_config
from intl_ratings.engines import (
    BondPriceEngine,
    FinancialsEngine,
    ListingEngine,
    NoRatingReasonGenerator,
    RatingsEngine,
    ratings_pending_notice,
)
from intl_ratings.engines.akshare_cn import AkshareCnClient
from intl_ratings.engines.sec_edgar import SecEdgarClient
from intl_ratings.engines.tvdatafeed_client import TvDatafeedClient
from intl_ratings.engines.playwright_ratings import PlaywrightRatingsScraper
from intl_ratings.io import load_issuer_list
from intl_ratings.issuers_store import IssuersStore
from intl_ratings.llm_client import LlmClient
from intl_ratings.logging_utils import ErrorIssuerLogger, RawResponseStore
from intl_ratings.mapper import EntityMapper
from intl_ratings.models import (
    NEED_REVIEW,
    NR,
    FinancialSnapshot,
    IssuerAnalysisResult,
    IssuerRiskModel,
    ListingSnapshot,
)
from intl_ratings.openfigi import OpenFigiClient
from intl_ratings.report import export_excel

logger = logging.getLogger(__name__)


class IntlRatingsPipeline:
    def __init__(self, config: Optional[IntlRatingsConfig] = None) -> None:
        self.cfg = config or get_intl_config()
        self.env = get_env()

        raw_dir = self.cfg.resolve(self.cfg.paths.raw_response_dir)
        err_path = self.cfg.resolve(self.cfg.paths.error_log)
        self.raw_store = RawResponseStore(raw_dir)
        self.error_log = ErrorIssuerLogger(err_path)

        timeout = self.cfg.runtime.request_timeout_seconds
        self.issuers_store = IssuersStore(self.cfg.resolve(self.cfg.paths.issuers_json))
        self.llm = LlmClient(
            env=self.env,
            raw_store=self.raw_store,
            timeout=timeout,
            temperature=self.cfg.entity_mapper.temperature,
        )
        openfigi = None
        if self.cfg.sources.openfigi:
            openfigi = OpenFigiClient(
                api_key=self.env.openfigi_api_key,
                timeout=timeout,
                raw_store=self.raw_store,
            )

        self.mapper = EntityMapper(
            self.cfg.entity_mapper,
            cache_path=self.cfg.resolve(self.cfg.paths.entity_cache),
            issuers_store=self.issuers_store,
            openfigi=openfigi,
            llm=self.llm,
            raw_store=self.raw_store,
            env=self.env,
            timeout=timeout,
        )
        self.ratings = RatingsEngine(
            self.cfg.sources,
            raw_store=self.raw_store,
            error_log=self.error_log,
            placeholders_nr=self.cfg.placeholders.nr,
            playwright_scraper=(
                PlaywrightRatingsScraper(
                    headless=self.cfg.playwright_ratings.headless,
                    timeout_ms=self.cfg.playwright_ratings.timeout_ms,
                    raw_store=self.raw_store,
                    error_log=self.error_log,
                    enable_agency_pages=self.cfg.playwright_ratings.enable_agency_pages,
                )
                if self.cfg.sources.playwright_ratings
                else None
            ),
        )
        self.financials = FinancialsEngine(
            self.cfg.sources,
            raw_store=self.raw_store,
            error_log=self.error_log,
        )
        self.listing = ListingEngine(
            self.cfg.sources,
            raw_store=self.raw_store,
            error_log=self.error_log,
            not_listed_label=self.cfg.placeholders.not_listed,
        )

        self.tv_client = None
        if self.cfg.sources.tvdatafeed:
            self.tv_client = TvDatafeedClient(
                username=self.env.tradingview_username,
                password=self.env.tradingview_password,
                raw_store=self.raw_store,
                error_log=self.error_log,
            )
        self.bonds = BondPriceEngine(
            self.cfg.sources,
            self.cfg.bond_price,
            raw_store=self.raw_store,
            error_log=self.error_log,
            no_public_label=self.cfg.placeholders.no_public_trade,
            tv_client=self.tv_client,
        )
        self.reason_gen = NoRatingReasonGenerator(
            env=self.env,
            raw_store=self.raw_store,
            timeout=timeout,
            llm=self.llm,
        )
        self.ak_cn = AkshareCnClient(raw_store=self.raw_store, error_log=self.error_log)
        self.sec_edgar = None
        if self.cfg.sources.sec_edgar:
            self.sec_edgar = SecEdgarClient(
                download_dir=self.cfg.resolve(self.cfg.paths.sec_edgar_dir),
                company_name=self.env.sec_edgar_company,
                email=self.env.sec_edgar_email,
                raw_store=self.raw_store,
                error_log=self.error_log,
            )

    def analyze_one(self, issuer_name: str) -> IssuerAnalysisResult:
        mapping = self.mapper.map(issuer_name)

        # 国内公告/货币网：AkShare（溯源落盘）
        if self.cfg.sources.akshare and not self.cfg.runtime.market_only:
            try:
                self.ak_cn.fetch_cn_disclosures(mapping)
            except Exception as exc:
                logger.debug("AkShare 国内披露失败: %s", exc)

        # 美股 SEC 财报下载（有 us_ticker/cik 时）
        if self.sec_edgar is not None and not self.cfg.runtime.market_only:
            try:
                self.sec_edgar.download_filings(mapping)
            except Exception as exc:
                logger.debug("SEC EDGAR 失败: %s", exc)

        ratings = self.ratings.fetch(mapping)
        if self.cfg.runtime.market_only:
            financials = FinancialSnapshot()
            listing = ListingSnapshot()
        else:
            financials = self.financials.fetch(mapping)
            listing = self.listing.fetch(mapping)
        bond_price = self.bonds.fetch(mapping)

        no_reason = ""
        if ratings.all_blank_or_nr():
            no_reason = self.reason_gen.generate(mapping)

        return IssuerAnalysisResult(
            mapping=mapping,
            ratings=ratings,
            financials=financials,
            listing=listing,
            bond_price=bond_price,
            no_rating_reason=no_reason,
        )

    def run(
        self,
        issuers: Optional[list[str]] = None,
        *,
        export: bool = True,
    ) -> tuple[list[IssuerRiskModel], Optional[Path]]:
        logger.info(ratings_pending_notice())

        if issuers is None:
            input_dir = self.cfg.resolve(self.cfg.paths.input_dir)
            _, issuers = load_issuer_list(input_dir, self.cfg.input_files)

        max_n = int(self.cfg.runtime.max_issuers or 0)
        if max_n > 0:
            issuers = issuers[:max_n]

        rows: list[IssuerRiskModel] = []
        pause = float(self.cfg.runtime.sleep_between_issuers or 0)

        for i, name in enumerate(issuers, start=1):
            logger.info("[%d/%d] 分析发行体: %s", i, len(issuers), name)
            try:
                result = self.analyze_one(name)
                row = result.to_report_row()
                rows.append(IssuerRiskModel.model_validate(row.model_dump(by_alias=True)))
            except Exception as exc:
                logger.exception("分析失败: %s", name)
                self.error_log.log(name, "pipeline", str(exc))
                rows.append(
                    IssuerRiskModel.model_validate(
                        {
                            "发行体": name,
                            "穆迪评级": NR,
                            "标普评级": NR,
                            "惠誉评级": NR,
                            "债务人最近一期決算是否亏损(是/否)": NEED_REVIEW,
                            "是否上市（是/否）": NEED_REVIEW,
                            "若上市，债务人是否被上市废止(是/否)": NEED_REVIEW,
                            "债券价格是否大幅下跌（月环比跌幅超过5%）等": NEED_REVIEW,
                            "皆无评级的話请写明理由": f"流水线异常: {exc}",
                            "评级是否变化": NEED_REVIEW,
                        }
                    )
                )
            if pause > 0 and i < len(issuers):
                time.sleep(pause)

        out_path: Optional[Path] = None
        if export:
            out_dir = self.cfg.resolve(self.cfg.paths.output_dir)
            out_path = export_excel(rows, out_dir)
            logger.info("报表已导出: %s", out_path)

        return rows, out_path
