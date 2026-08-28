"""国际评级监测流水线入口（界面四）。

用法（项目根目录）:
  python -m intl_ratings.main
  python -m intl_ratings.main --limit 5
  python -m intl_ratings.main --probe-libs
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from intl_ratings.config import get_env, get_intl_config
from intl_ratings.io.issuer_loader import load_issuers_from_csv, load_issuers_from_docx
from intl_ratings.pipeline import IntlRatingsPipeline


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def probe_libs() -> int:
    """探测四个开源库是否可导入，并做最小调用示例。"""
    print("=== 开源库探测 ===")
    # 1) akshare
    try:
        import akshare as ak

        print(f"[OK] akshare {getattr(ak, '__version__', '')}")
        fn = getattr(ak, "stock_financial_abstract", None)
        print(f"     stock_financial_abstract: {'yes' if callable(fn) else 'missing'}")
        fn2 = getattr(ak, "stock_zh_a_disclosure_report_cninfo", None)
        print(f"     stock_zh_a_disclosure_report_cninfo: {'yes' if callable(fn2) else 'missing'}")
    except Exception as exc:
        print(f"[FAIL] akshare: {exc}")

    # 2) yfinance
    try:
        import yfinance as yf

        t = yf.Ticker("AAPL")
        ni = None
        try:
            fin = t.financials
            if fin is not None and not fin.empty and "Net Income" in fin.index:
                ni = float(fin.loc["Net Income"].iloc[0])
        except Exception:
            pass
        hist = t.history(period="1mo")
        print(f"[OK] yfinance hist_rows={len(hist)} net_income={ni}")
    except Exception as exc:
        print(f"[FAIL] yfinance: {exc}")

    # 3) sec-edgar-downloader
    try:
        from sec_edgar_downloader import Downloader

        env = get_env()
        out = _ROOT / "data" / "intl_ratings" / "sec_edgar" / "_probe"
        out.mkdir(parents=True, exist_ok=True)
        dl = Downloader(env.sec_edgar_company, env.sec_edgar_email, str(out))
        n = dl.get("10-K", "AAPL", limit=1)
        print(f"[OK] sec-edgar-downloader downloaded={n} -> {out}")
    except Exception as exc:
        print(f"[FAIL] sec-edgar-downloader: {exc}")

    # 4) tvDatafeed
    try:
        try:
            from tvDatafeed import Interval, TvDatafeed
        except ImportError:
            from tvdatafeed import Interval, TvDatafeed  # type: ignore

        env = get_env()
        if env.tradingview_username and env.tradingview_password:
            tv = TvDatafeed(username=env.tradingview_username, password=env.tradingview_password)
        else:
            tv = TvDatafeed()
        df = tv.get_hist(symbol="AAPL", exchange="NASDAQ", interval=Interval.in_daily, n_bars=5)
        print(f"[OK] tvDatafeed rows={0 if df is None else len(df)}")
    except Exception as exc:
        print(f"[FAIL] tvDatafeed: {exc}")
        print("      提示: pip install git+https://github.com/rongardF/tvdatafeed.git")
        print("      可选配置 TRADINGVIEW_USERNAME / TRADINGVIEW_PASSWORD")

    # 5) playwright
    try:
        from playwright.sync_api import sync_playwright
        from intl_ratings.engines.playwright_ratings import parse_ratings_from_text

        sample = "穆迪主体评级 Baa1，标普 BBB+，惠誉 A-"
        parsed = parse_ratings_from_text(sample)
        print(f"[OK] playwright import ok; parse sample={parsed}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        print("[OK] playwright chromium launch ok")
    except Exception as exc:
        print(f"[FAIL] playwright: {exc}")
        print("      提示: pip install playwright && playwright install chromium")

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="国际评级监测：发行体清单 → Excel 报表")
    p.add_argument("--config", default=None, help="配置文件路径")
    p.add_argument("--input", default=None, help="发行体清单 .csv / .docx")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 家")
    p.add_argument("--no-export", action="store_true")
    p.add_argument(
        "--probe-libs",
        action="store_true",
        help="仅探测 akshare / yfinance / sec-edgar-downloader / tvDatafeed",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    if args.probe_libs:
        return probe_libs()

    get_intl_config.cache_clear()
    cfg = get_intl_config(args.config)
    if args.limit and args.limit > 0:
        cfg = cfg.model_copy(deep=True)
        cfg.runtime.max_issuers = args.limit

    issuers: list[str] | None = None
    if args.input:
        path = Path(args.input)
        if not path.is_file():
            logging.error("清单文件不存在: %s", path)
            return 1
        suffix = path.suffix.lower()
        if suffix == ".csv":
            issuers = load_issuers_from_csv(path)
        elif suffix in {".docx", ".doc"}:
            issuers = load_issuers_from_docx(path)
        else:
            logging.error("仅支持 .csv / .docx")
            return 1
        logging.info("从 %s 加载 %d 家发行体", path, len(issuers))

    pipeline = IntlRatingsPipeline(cfg)
    rows, out = pipeline.run(issuers=issuers, export=not args.no_export)

    print(f"完成: {len(rows)} 行")
    if out:
        print(f"Excel: {out}")
    print(f"原始报文: {cfg.resolve(cfg.paths.raw_response_dir)}")
    print(f"错误日志: {cfg.resolve(cfg.paths.error_log)}")
    print(f"SEC下载目录: {cfg.resolve(cfg.paths.sec_edgar_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
