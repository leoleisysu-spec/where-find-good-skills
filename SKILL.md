---
name: where-find-good-skills
description: 在 GitHub 上按分类或功能关键词搜罗优质 Agent skill 仓库，并按星标数、更新频率、README 内容容量三维加权排行，输出仓库名、链接与功能简介。当用户要求"在 GitHub 上找 xx 相关的 skill / 搜罗优秀 skills / agent skill 推荐与排行 / 按星标搜 skill / 哪个 skill 仓库最火"时使用，关键词可用中文或英文。
---

# where-find-good-skills（简称 WFGoodSkills）

通过 GitHub 官方 Search API 检索 Agent skill 仓库并三维排行。低消耗：典型任务 3 次搜索请求（免登录配额 10 次/分钟）+ 25 次 raw README 抓取（零 API 配额）。大合集（awesome/collection/library 等）单独成榜，不与独立技能同榜竞争。

## 执行流程

1. 从用户请求中解析关键词 `keyword`（中英文均可，中文走内置映射表）；如用户指定条数/排序，解析 `--top`（默认 20，上限 50）与 `--sort`。
2. 运行主脚本：
   ```bash
   python3 scripts/search_skills.py "<keyword>" [--top 20] [--sort combo] [--json 结果.json]
   ```
3. 将脚本输出的 Markdown 表格原样呈现给用户，保留表前的统计行（召回 → 候选 → 展示 → README 成功率），成本透明。
4. 若用户要求保存结果，加 `--json <路径>` 输出结构化 JSON，或把表格另存为 .md 文件交付。

## 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `keyword` | ✅ | 分类/功能关键词，如 `PPT`、`数据可视化`、`excel`、`video` |
| `--top` | 可选 | 展示条数，默认 20，上限 50 |
| `--sort` | 可选 | `combo`（综合，默认）｜`stars` 星标榜｜`active` 活跃榜｜`detail` 容量榜｜`rising` 年内热门榜 |
| `--json` | 可选 | 额外输出结构化 JSON 到指定路径 |

## 输出规范

输出**两个榜单**：`## 独立技能综合榜`（主榜，单点技能仓库）与 `## 精选合集综合榜`（大合集单列对比，避免大合集长期霸榜、挤压独立技能曝光）。合集榜每行附加"内部亮点"列：从 README 抽取的 Top 3 子技能条目 + 估算的子技能条目数。

表格列：`# 排名 | 仓库[owner/repo](链接) | 简介(截断60字符) | ★(千分位) | 更新(N天前,≥730天显示"＞2年") | 容量(README KB,抓取失败显示"–") | 分(综合分0.000~1.000,仅combo显示)`。

综合分公式：`0.5·星标归一化(log₁₀(★+1)) + 0.3·活跃度(e^(−距今天数/180)) + 0.2·容量归一化(log₁₀(README字符+1))`。单维榜（`--sort`）只切换排序键，格式不变。

## 关键词映射

中文分类 → 英文同义词的映射表内置在 `scripts/search_skills.py` 的 `KEYWORD_MAP` 中（约 20 类：PPT/图表/Excel/文档/绘图/视频/音频/爬虫/翻译/写作/代码/数据分析/营销/邮件/日历/搜索/测试/部署/安全等）。新增分类时直接编辑该 dict，每类 1~3 个英文词。

## 行为约定（脚本已内置，无需人工干预）

- **召回降级**：先跑标准模板 `"{kw}" skills fork:false`；候选不足 20 时自动补 `topic:claude-skills/topic:agent-skills` 锚定模板与兜底模板，总搜索请求控制在 6 次以内。
- **精筛**：剔除 fork、archived、空描述；仓库名/描述/topics 必须含 skill 信号词；描述含工具词（harness/workbench/framework/sdk 等）且无 skill 信号词的仓库会被剔除。
- **合集识别**：仓库名含 awesome/collection/library/bundle/hub/gallery/marketplace 等词，或 topics/描述含"curated/collection of/bundle of"等合集表述的，判为大合集，从独立榜移入合集榜；合集榜固定展示前 5 个。
- **无效仓库剔除**：README 抓取失败且星标 < 300 的（多为空壳/随机生成名仓库）直接从两榜移除。
- **年内热门榜**：用户想看新兴/热门技能时用 `--sort rising`，按"一年内创建（≤365 天）+ 两周内有更新（≤14 天）"筛选，独立榜与合集榜同步适用，筛选后按综合分排序；条件为脚本顶部常量 `RISING_CREATED_DAYS=365` / `RISING_PUSHED_DAYS=14`，可按需编辑。筛选结果为空时提示用户换关键词或退回常规综合榜。
- **限流**：未认证配额 10 次搜索/分钟，脚本对 403/429 自动退避（最长等 60s）。若连续两次运行均提示配额不足，等待约 1 分钟再试。
- **失败容忍**：README 抓取失败不影响排行（容量列显示 "–"）；单次查询失败自动重试 2 次。
- **结果质量**：综合分不等于星标排名——星标高但长期不更新、README 单薄的仓库会被拉低；独立榜与合集榜各自归一化评分，互不干扰；向用户说明榜单按综合分排序即可。

## 典型输出示例（关键词 数据可视化）

```
召回 143 → 独立技能候选 128 → 合集候选 14 · 独立榜展示 8（README 8/8） · 合集展示 3（README 3/3） · 搜索请求 3 次 · 2026-10-24

## 独立技能综合榜 Top 8
| # | 仓库 | 简介 | ★ | 更新 | 容量 | 分 |
|---|------|------|----|------|------|-----|
| 1 | [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) | Claude Code skills for equity investors and traders — market… | 3,243 | 3天前 | 47.1KB | 0.815 |

## 精选合集综合榜（大合集单列对比，避免霸榜）
| # | 仓库 | 简介 | ★ | 更新 | 容量 | 分 | 内部亮点 |
|---|------|------|----|------|------|-----|----------|
| 1 | [alvinreal/awesome-openclaw](https://github.com/alvinreal/awesome-openclaw) | A curated list of the best OpenClaw resources… | 744 | 8天前 | 54.1KB | 1.000 | 亮点: openclaw/openclaw、openclaw.ai · 含 ~99 个子技能条目 |
```
