"""国际评级：快照读写与后台刷新任务。"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

try:
    from intl_ratings.config import get_intl_config
    from intl_ratings.io import load_issuer_records
    from intl_ratings.models import NR
    from intl_ratings.pipeline import IntlRatingsPipeline
    _INTL_RATINGS_RUNTIME_ERROR: str | None = None
except ModuleNotFoundError as exc:
    # 国际评级的独立源码未随当前项目副本提交时，不阻断新闻和主体评估主站。
    # 访问该模块仍会显示占位数据，手动刷新任务则记录明确的缺失原因。
    get_intl_config = None  # type: ignore[assignment]
    load_issuer_records = None  # type: ignore[assignment]
    IntlRatingsPipeline = None  # type: ignore[assignment]
    NR = "NR"
    _INTL_RATINGS_RUNTIME_ERROR = str(exc)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = ROOT / "data" / "intl_ratings" / "latest.json"
# 手工台账与联网流水线分别留存：定时更新不应覆盖用户当前导入的监测口径。
MANUAL_SNAPSHOT_PATH = ROOT / "data" / "intl_ratings" / "manual_excel.json"
JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_RUNNING = False

CATEGORY_SIMPLE = "简易分类债券"
CATEGORY_NON_SIMPLE = "非简易分类债券"

# 与前端原清单对齐的分类兜底（CSV 无分类时使用）
_CATEGORY_FALLBACK: dict[str, str] = {
    "ABU DHABI COMMERCIAL BANK, ABU DHABI": CATEGORY_SIMPLE,
    "AGRICULTURAL DEVELOPMENT BANK OF CHINA, THE, BEIJING": CATEGORY_SIMPLE,
    "BARCLAYS BANK PLC (ALL U.K. OFFICES)": CATEGORY_SIMPLE,
    "CCBL(Cayman)1 Corporation Limited": CATEGORY_SIMPLE,
    "CDBL FUNDING 1": CATEGORY_SIMPLE,
    "CHINA CINDA FINANCE (2017) I LIMITED": CATEGORY_SIMPLE,
    "CSI_MTN_LIMITED": CATEGORY_SIMPLE,
    "DBS Bank Ltd, Australia Branch": CATEGORY_SIMPLE,
    "EMIRATES NBD BANK PJSC": CATEGORY_SIMPLE,
    "EXPORT-IMPORT BANK OF CHINA, THE, BEIJING": CATEGORY_SIMPLE,
    "EXPORT-IMPORT BANK OF KOREA, THE, SEOUL": CATEGORY_SIMPLE,
    "FIRST ABU DHABI BANK PJSC H.O.": CATEGORY_SIMPLE,
    "ICBCIL FINANCE CO. LIMITED": CATEGORY_SIMPLE,
    "INDUSTRIAL BANK OF KOREA": CATEGORY_SIMPLE,
    "KEB HANA BANK": CATEGORY_SIMPLE,
    "KOREA DEVELOPMENT BANK, THE, SEOUL": CATEGORY_SIMPLE,
    "MITSUBISHI HC CAPITAL INC": CATEGORY_SIMPLE,
    "MITSUBISHI HC CAPITAL UK PLC": CATEGORY_SIMPLE,
    "MIZUHO BANK, LTD": CATEGORY_SIMPLE,
    "NORINCHUKIN BANK,THE,TOKYO": CATEGORY_SIMPLE,
    "QNB Finance Ltd": CATEGORY_SIMPLE,
    "SHINHAN BANK, SEOUL": CATEGORY_SIMPLE,
    "SNB Funding Limited": CATEGORY_SIMPLE,
    "SOCIETE GENERALE, PARIS": CATEGORY_SIMPLE,
    "STANDARD CHARTERED BANK LONDON (ALL U.K. OFFICES)": CATEGORY_SIMPLE,
    "Sumitomo Mitsui Finance and Leasing Company, Limited": CATEGORY_SIMPLE,
    "WESTPAC BANKING CORPORATION": CATEGORY_SIMPLE,
    "交银租赁管理香港有限公司": CATEGORY_SIMPLE,
    "沙特阿拉伯王国政府": CATEGORY_SIMPLE,
    "三井住友信托银行股份有限公司": CATEGORY_SIMPLE,
    "中国光大银行股份有限公司卢森堡分行": CATEGORY_SIMPLE,
    "中银航空租赁有限公司": CATEGORY_SIMPLE,
    "法国BPCE银行": CATEGORY_SIMPLE,
    "法国国民互助信贷银行": CATEGORY_SIMPLE,
    "韩国政府": CATEGORY_SIMPLE,
    "CCCI TREASURE LIMITED": CATEGORY_NON_SIMPLE,
    "CHINA HUANENG GROUP CO., LTD.": CATEGORY_NON_SIMPLE,
    "CHINA SOUTHERN POWER GRID CO., LTD": CATEGORY_NON_SIMPLE,
    "CHINA THREE GORGES CORPORATION": CATEGORY_NON_SIMPLE,
    "CNOOC Limited": CATEGORY_NON_SIMPLE,
    "DENSO CORPORATION": CATEGORY_NON_SIMPLE,
    "Haitong UT Brilliant Limited": CATEGORY_NON_SIMPLE,
    "ITOCHU CORPORATION": CATEGORY_NON_SIMPLE,
    "MARUBENI CORPORATION": CATEGORY_NON_SIMPLE,
    "Mitsubishi Corporation": CATEGORY_NON_SIMPLE,
    "MITSUI & CO.,LTD.": CATEGORY_NON_SIMPLE,
    "ORIX CORPORATION": CATEGORY_NON_SIMPLE,
    "SUMITOMO CORPORATION": CATEGORY_NON_SIMPLE,
    "Suntory Holdings Limited": CATEGORY_NON_SIMPLE,
    "TAKEDA PHARMACEUTICAL COMPANY LIMITED": CATEGORY_NON_SIMPLE,
}


def _resolve_category(name: str, raw: str = "") -> str:
    if raw in (CATEGORY_SIMPLE, CATEGORY_NON_SIMPLE):
        return raw
    if "非简易" in (raw or ""):
        return CATEGORY_NON_SIMPLE
    if "简易" in (raw or ""):
        return CATEGORY_SIMPLE
    return _CATEGORY_FALLBACK.get(name, CATEGORY_NON_SIMPLE)


def _placeholder_row(idx: int, name: str, category: str) -> dict[str, Any]:
    return {
        "id": f"ir-{idx}",
        "issuer": name,
        "category": _resolve_category(name, category),
        "moodys": NR,
        "sp": NR,
        "fitch": NR,
        "loss": "[需人工复核]",
        "listed": "否",
        "delisted": "未上市",
        "priceDrop": "无公开交易数据",
        "noRatingReason": "",
        "ratingChanged": "否",
        "rssUrl": "",
    }


def _market_source_url(pipeline: Any, issuer_name: str) -> str:
    """为自动取得股票市场代理的发行体提供可直接复核的公开历史行情链接。"""
    try:
        record = pipeline.issuers_store.get(issuer_name)
        ticker = (record.stock_ticker if record else "") or ""
        if ticker:
            return "https://finance.yahoo.com/quote/" + quote(ticker, safe=".") + "/history"
    except Exception:
        pass
    return ""


def _rating_change_label(current: dict[str, Any], previous: Optional[dict[str, Any]]) -> str:
    """仅在两期均有正式评级且等级不同的情况下提示变动。"""
    if not previous:
        return "否"
    changed: list[str] = []
    labels = (("moodys", "穆迪"), ("sp", "标普"), ("fitch", "惠誉"))
    for key, label in labels:
        before = str(previous.get(key) or "").strip()
        after = str(current.get(key) or "").strip()
        # NR / 空值只是尚未核验，并不代表评级变化。
        if not before or not after or before.upper() == "NR" or after.upper() == "NR":
            continue
        if before != after:
            changed.append(f"{label} {before}→{after}")
    return "；".join(changed) if changed else "否"


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("rows"), list):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取评级快照失败: %s", exc)
    return None


def load_snapshot() -> dict[str, Any]:
    # 用户手动导入的 Excel 是当前监测面板的明确数据源，优先级高于后台定时刷新。
    # 联网流水线仍保存到 latest.json，便于后续人工切换或复核。
    data = _read_snapshot(MANUAL_SNAPSHOT_PATH) or _read_snapshot(SNAPSHOT_PATH)
    if data:
        data["running"] = _is_running()
        return data
    return _build_skeleton_snapshot()


def _build_skeleton_snapshot() -> dict[str, Any]:
    """尚无流水线结果时，用清单生成占位行，避免前端空白。"""
    rows: list[dict[str, Any]] = []
    try:
        if get_intl_config is None or load_issuer_records is None:
            raise RuntimeError(_INTL_RATINGS_RUNTIME_ERROR or "国际评级运行组件未安装")
        cfg = get_intl_config()
        input_dir = cfg.resolve(cfg.paths.input_dir)
        _, records = load_issuer_records(input_dir, cfg.input_files)
        for i, rec in enumerate(records, start=1):
            rows.append(_placeholder_row(i, rec["name"], rec.get("category") or ""))
    except Exception:
        # 回退：分类表全量占位
        for i, (name, cat) in enumerate(_CATEGORY_FALLBACK.items(), start=1):
            rows.append(_placeholder_row(i, name, cat))
    return {
        "updated_at": None,
        "source": "skeleton",
        "message": "尚未运行评级流水线，当前为占位数据。请点击「手动更新」。",
        "rows": rows,
        "running": _is_running(),
    }


def save_snapshot(rows: list[dict[str, Any]], *, source: str = "pipeline") -> Path:
    path = MANUAL_SNAPSHOT_PATH if source == "manual_excel" else SNAPSHOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "message": "",
        "rows": rows,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _excel_text(value: Any) -> str:
    """将 Excel 单元格值安全地转成适合监测表展示的文本。"""
    if value is None:
        return ""
    return str(value).strip()


def _excel_first(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = _excel_text(row.get(name))
        if value:
            return value
    return ""


def _normalise_import_rating(value: str) -> str:
    """清理 Bloomberg 导出中附带的非评级展示后缀，不改变 WD/NR 等原始状态。"""
    rating = value.strip()
    if len(rating) > 1 and rating.endswith("u") and rating[:-1].replace("+", "").replace("-", "").isalpha():
        return rating[:-1]
    return rating


def _excel_number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _excel_market_signal(row: dict[str, Any]) -> str:
    """按导入表中的收盘价变动生成可复核市场预警。

    仅使用上传文件已有的 1 日、5 日、1 月变动字段；未取得行情时不推断。
    阈值与页面既有“月环比跌幅超过 5%”口径保持一致。
    """
    one_month = _excel_number(row.get("%1M"))
    five_days = _excel_number(row.get("%5D"))
    if one_month is not None and one_month <= -5:
        return f"近 1 月 {one_month:.1f}%（跌幅超过 5%）"
    if five_days is not None and five_days <= -3:
        return f"近 5 日 {five_days:.1f}%（跌幅超过 3%）"
    if one_month is None and five_days is None:
        return "无公开交易数据"
    return "未见市场预警"


def import_excel_snapshot(content: bytes, *, filename: str = "") -> dict[str, Any]:
    """读取手动上传的评级 Excel 并替换当前国际评级快照。

    该导入仅消费用户上传的字段，不会补造评级或市场数据。发行体重复时合并为
    一条监测记录，并优先保留三大评级更完整的那一行。
    """
    try:
        from openpyxl import load_workbook
    except ModuleNotFoundError as exc:  # pragma: no cover - requirements 已声明
        raise ValueError("缺少 openpyxl，无法读取 Excel 文件") from exc

    if not content:
        raise ValueError("上传文件为空")
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        worksheet = workbook.active
        raw_headers = next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
    except Exception as exc:
        raise ValueError("无法读取 Excel，请确认文件未损坏且首行为字段名") from exc

    headers = [_excel_text(value) for value in raw_headers]
    if not any(headers):
        raise ValueError("Excel 首行缺少字段名")
    if not any(name in headers for name in ("发行人名称", "名称", "简体中文全称")):
        raise ValueError("Excel 缺少发行人名称、名称或简体中文全称字段")

    previous_by_issuer = {
        str(item.get("issuer") or ""): item
        for item in (load_snapshot().get("rows") or [])
        if isinstance(item, dict)
    }
    grouped: dict[str, dict[str, Any]] = {}
    for values in worksheet.iter_rows(min_row=2, values_only=True):
        record = {headers[index]: values[index] if index < len(values) else None for index in range(len(headers))}
        issuer = _excel_first(record, "发行人名称", "名称", "简体中文全称")
        if not issuer:
            continue
        moodys = _normalise_import_rating(_excel_first(record, "穆迪长期评级", "穆迪发行人评级", "穆迪")) or NR
        sp = _normalise_import_rating(_excel_first(record, "S&P LT外币发行信用评级", "S&P LT LC", "标普")) or NR
        fitch = _normalise_import_rating(_excel_first(record, "惠誉长期发行人违约评级", "惠誉高级无抵押债评级", "惠誉评级")) or NR
        category = _resolve_category(issuer, _excel_first(record, "分类", "债券种类"))
        candidate = {
            "issuer": issuer,
            "category": category,
            "moodys": moodys,
            "sp": sp,
            "fitch": fitch,
            "loss": _excel_first(record, "净利润"),
            "listed": "是" if _excel_first(record, "代码", "证券", "彭博编码") else "",
            "delisted": "",
            "priceDrop": _excel_market_signal(record),
            "noRatingReason": "",
            "ratingChanged": "否",
            "ratingSourceUrl": "",
            "rssUrl": "",
            "sourceLabel": "手动上传 Excel" + (f" · {filename}" if filename else ""),
        }
        existing = grouped.get(issuer)
        if not existing or sum(value.upper() != NR for value in (moodys, sp, fitch)) > sum(
            value.upper() != NR for value in (existing["moodys"], existing["sp"], existing["fitch"])
        ):
            grouped[issuer] = candidate

    if not grouped:
        raise ValueError("未在 Excel 中读取到有效发行体记录")

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(sorted(grouped.values(), key=lambda row: row["issuer"].casefold()), start=1):
        item["id"] = f"excel-{index}"
        item["ratingChanged"] = _rating_change_label(item, previous_by_issuer.get(item["issuer"]))
        rows.append(item)

    save_snapshot(rows, source="manual_excel")
    return {
        "rows": rows,
        "issuer_count": len(rows),
        "message": f"已导入 {len(rows)} 家发行体" + (f"：{filename}" if filename else ""),
    }


def _is_running() -> bool:
    with _LOCK:
        return _RUNNING


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def start_refresh_job(*, limit: int = 0, quick: bool = False) -> dict[str, Any]:
    global _RUNNING
    with _LOCK:
        if _RUNNING:
            for jid, job in JOBS.items():
                if job.get("status") in {"queued", "running"}:
                    return {
                        "job_id": jid,
                        "status": job["status"],
                        "message": "已有刷新任务在运行",
                        "accepted": False,
                    }
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "message": "任务已排队",
            "total": 0,
            "done": 0,
            "error": "",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }
        _RUNNING = True

    t = threading.Thread(
        target=_run_job,
        kwargs={"job_id": job_id, "limit": limit, "quick": quick},
        daemon=True,
        name=f"intl-ratings-{job_id}",
    )
    t.start()
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "已开始刷新国际评级",
        "accepted": True,
    }


def _run_job(*, job_id: str, limit: int, quick: bool) -> None:
    global _RUNNING
    try:
        if (
            get_intl_config is None
            or load_issuer_records is None
            or IntlRatingsPipeline is None
        ):
            raise RuntimeError(
                "国际评级模块源码缺失，请恢复 intl_ratings/ 目录后再刷新："
                + (_INTL_RATINGS_RUNTIME_ERROR or "未知原因")
            )
        with _LOCK:
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["message"] = "正在抓取与分析…"

        previous_by_issuer = {
            str(item.get("issuer") or ""): item
            for item in (load_snapshot().get("rows") or [])
            if isinstance(item, dict)
        }

        get_intl_config.cache_clear()
        cfg = get_intl_config().model_copy(deep=True)
        if limit and limit > 0:
            cfg.runtime.max_issuers = int(limit)
        if quick:
            # 页面手动刷新只更新三大评级与市场信号；跳过不在当前面板展示的
            # 财务、国内公告、TradingView 与网页自动化探测，避免整页长时间阻塞。
            cfg.sources.playwright_ratings = False
            cfg.sources.openfigi = False
            cfg.sources.sec_edgar = False
            cfg.sources.akshare = False
            cfg.sources.tvdatafeed = False
            cfg.entity_mapper.use_llm = False
            cfg.runtime.sleep_between_issuers = 0.1
            cfg.runtime.market_only = True

        # 分类映射
        input_dir = cfg.resolve(cfg.paths.input_dir)
        try:
            _, records = load_issuer_records(input_dir, cfg.input_files)
            cat_map = {r["name"]: r.get("category") or "" for r in records}
            names = [r["name"] for r in records]
        except Exception:
            # 项目未附带 CSV 时仍按界面内置发行体清单执行，避免“手动更新”空跑。
            cat_map = dict(_CATEGORY_FALLBACK)
            # 先处理已有实体映射的主体，提高免费公开检索命中率；其余清单仍保留。
            mapped_names: list[str] = []
            try:
                mappings = json.loads(cfg.resolve(cfg.paths.issuers_json).read_text(encoding="utf-8"))
                mapped_names = [name for name in (mappings.get("issuers") or {}) if name in cat_map]
            except Exception:
                pass
            names = mapped_names + [name for name in _CATEGORY_FALLBACK if name not in mapped_names]

        if limit and limit > 0 and names:
            names = names[:limit]

        pipeline = IntlRatingsPipeline(cfg)
        report_rows, excel_path = pipeline.run(issuers=names, export=True)

        api_rows: list[dict[str, Any]] = []
        for i, row in enumerate(report_rows, start=1):
            d = row.to_excel_dict()
            name = d.get("发行体") or ""
            api_row = {
                    "id": f"ir-{i}",
                    "issuer": name,
                    "category": _resolve_category(name, cat_map.get(name, "")),
                    "moodys": d.get("穆迪评级") or NR,
                    "sp": d.get("标普评级") or NR,
                    "fitch": d.get("惠誉评级") or NR,
                    "loss": d.get("债务人最近一期決算是否亏损(是/否)") or "",
                    "listed": d.get("是否上市（是/否）") or "",
                    "delisted": d.get("若上市，债务人是否被上市废止(是/否)") or "",
                    "priceDrop": d.get("债券价格是否大幅下跌（月环比跌幅超过5%）等") or "",
                    "noRatingReason": d.get("皆无评级的話请写明理由") or "",
                    "ratingChanged": d.get("评级是否变化") or "否",
                    "ratingSourceUrl": (getattr(row, "rating_source_urls", None) or [""])[0],
                    "rssUrl": _market_source_url(pipeline, name),
                }
            api_row["ratingChanged"] = _rating_change_label(
                api_row, previous_by_issuer.get(name)
            )
            api_rows.append(api_row)

        save_snapshot(api_rows, source="pipeline_quick" if quick else "pipeline")
        with _LOCK:
            JOBS[job_id].update(
                {
                    "status": "succeeded",
                    "message": f"完成 {len(api_rows)} 家"
                    + (f"；Excel: {excel_path.name}" if excel_path else ""),
                    "total": len(api_rows),
                    "done": len(api_rows),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "excel": str(excel_path) if excel_path else "",
                }
            )
    except Exception as exc:
        logger.exception("国际评级刷新失败")
        with _LOCK:
            JOBS[job_id].update(
                {
                    "status": "failed",
                    "message": "刷新失败",
                    "error": str(exc),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    finally:
        with _LOCK:
            _RUNNING = False
