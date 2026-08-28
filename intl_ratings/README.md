# 国际评级监测流水线（界面四）

## 公式口径

- 亏损：归母净利润 `< 0` → 是；`>= 0` → 否；离岸 SPV 继承母公司/担保人
- 债券月环比：`((当前收盘 - 30日前收盘) / 30日前收盘) * 100`；`<= -5%` → 是；无行情 → 无公开交易数据
- 上市：有有效股票代码 → 是；退市判定含 Delisted / Suspended / ST / *ST
- 皆无评级理由：穆迪=标普=惠誉=`NR` 时触发 LLM

## 目录

```
config/issuers.json          # 发行体映射主表
config/intl_ratings.yaml     # 开关与路径
intl_ratings/main.py         # 入口
logs/raw_responses/          # API 原始报文
logs/error.log               # 异常发行体
data/intl_ratings/input|output
```

## 开源库

| 库 | 用途 |
|----|------|
| `akshare` | 巨潮公告、A股财报、货币网相关截面 |
| `yfinance` | 海外归母净利润、历史价格、上市状态 |
| `sec-edgar-downloader` | 美股 10-K/10-Q |
| `tvdatafeed` | TradingView 债券/标的日线 |
| `playwright` | 未上市主体评级公开页兜底（新浪/东财/机构） |

探测：`python -m intl_ratings.main --probe-libs`

首次使用 Playwright 需安装浏览器：

```powershell
python -m pip install playwright
playwright install chromium
```
