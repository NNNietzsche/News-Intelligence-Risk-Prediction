# UI 样式维护说明

> 修改人：DingJiaye  
> 建立日期：2026-08-25

## 文件职责

| 文件 | 负责范围 | 后续修改位置 |
| --- | --- | --- |
| `app/static/css/dashboard.css` | 历史兼容 | 只修旧样式，不新增功能 |
| `app/static/css/ui/tokens.css` | 视觉变量 | 色板、边框、圆角、阴影 |
| `app/static/css/ui/layout.css` | 通用布局 | 顶栏、侧栏、分栏、响应式 |
| `app/static/css/ui/daily-news.css` | 新闻汇总 | 新闻卡、筛选、时间轴、空状态 |
| `app/static/css/ui/entity-assessment.css` | 主体评估 | 概览、事件、财务展示 |
| `app/static/css/ui/industry-analysis.css` | 行业分析 | 索引、信源、报告、对话栏 |
| `app/static/css/ui/intl-ratings.css` | 国际评级 | 评级、变动、市场信号 |

## 修改规则

1. **只改对应页面文件**：例如修改大型企业新闻卡片，使用 `ui/daily-news.css`。
2. **先使用变量**：颜色、边框、文字和圆角优先使用 `tokens.css` 的 `--ui-*` 变量。
3. **避免无范围选择器**：不要写 `h2`、`button` 等全局选择器；使用页面容器或组件类限定范围。
4. **保持加载顺序**：`base.html` 先加载兼容层 `dashboard.css`，再加载 `ui/*.css`；不要改变顺序。
5. **每次改动必须记录**：在所修改 CSS 文件顶部的“修改记录”新增一行。

## 改动记录格式

```css
/*
 * 修改记录
 * - YYYY-MM-DD | DingJiaye | 修改内容与影响范围。
 */
```

示例：

```css
/*
 * 修改记录
 * - 2026-08-26 | DingJiaye | 调整大型企业新闻卡片的标题字号与风险色条。
 */
```

## 迁移策略

历史样式量较大，采用**兼容层 + 增量迁移**：现有页面不改视觉与优先级；今后的新增或调整先写入独立文件。每次修改某个历史组件时，可将该组件完整规则从 `dashboard.css` 移至对应 `ui/*.css`，并在两处留下迁移记录。
