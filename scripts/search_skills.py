#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""where-find-good-skills（WFGoodSkills）—— 按关键词搜罗 GitHub 上的 Agent skill 仓库并三维排行。

流程：中文映射 → 搜索召回(GitHub REST Search API, sort=stars)
      → 本地精筛(去 fork/archived/空描述, skill 信号判定, 黑名单)
      → 分流：独立技能榜 / 精选合集榜（大合集单列，避免长期霸榜）
      → Top N 抓 README(raw.githubusercontent, 零 API 配额)，合集额外抽取内部子技能亮点
      → 三维评分(星标 / 更新频率 / README 容量) → 综合榜 + 单维榜

依赖：Python 3.8+ 标准库（urllib），无需第三方包；免登录配额 10 次搜索/分钟。

用法：
    python3 search_skills.py <keyword> [--top 20] [--sort combo] [--json out.json]
    --top   独立榜展示条数，默认 20，上限 50（合集榜固定展示前 5）
    --sort  combo(综合,默认) | stars(星标) | active(活跃) | detail(容量)
    --json  可选，额外输出结构化 JSON 到指定路径
"""
import argparse
import datetime
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "WFGoodSkills", "Accept": "application/vnd.github+json"}
NOW = datetime.datetime.now(datetime.timezone.utc)

# 中文常见分类 → 英文同义词（扩展请直接编辑本表，每类 1~3 词）
KEYWORD_MAP = {
    "ppt": ["presentation", "slides", "deck"],
    "演示": ["presentation", "slides", "deck"],
    "数据可视化": ["data visualization", "chart", "dashboard"],
    "图表": ["chart", "diagram", "dashboard"],
    "excel": ["excel", "spreadsheet"],
    "表格": ["spreadsheet", "excel", "table"],
    "文档": ["document", "docx", "writing"],
    "word": ["document", "docx", "writing"],
    "绘图": ["diagram", "drawing", "illustration"],
    "图片": ["image", "drawing", "design"],
    "视频": ["video", "editing", "movie"],
    "音频": ["audio", "podcast", "voice"],
    "爬虫": ["scraper", "crawl", "spider"],
    "翻译": ["translation", "translate"],
    "写作": ["writing", "copywriting"],
    "代码": ["coding", "programming", "code review"],
    "数据分析": ["data analysis", "analytics"],
    "营销": ["marketing", "seo"],
    "邮件": ["email"],
    "日历": ["calendar"],
    "搜索": ["search", "research"],
    "测试": ["testing", "test"],
    "部署": ["devops", "deploy", "server"],
    "安全": ["security", "pentest"],
}

SKILL_SIGNAL = re.compile(r"\bskills?\b|claude-skills|agent-skills|awesome-.*-skills", re.I)
# 工具类误召回黑名单：仅当描述中无 skill 信号词时才生效
BLACKLIST = re.compile(
    r"\b(harness|manager|monitor|platform|framework|sdk|workbench|studio|middleware|runtime)\b", re.I)

# ---------- 年内热门榜（--sort rising）筛选条件 ----------
RISING_CREATED_DAYS = 365   # 一年内创建
RISING_PUSHED_DAYS = 14     # 两周内有更新

# ---------- 大合集识别（合集单列，不与独立技能同榜竞争） ----------
# 名字含合集特征词，或 topics 含 awesome/collection 等，或描述含明确的"合集"表述
COLLECTION_NAME = re.compile(r"(awesome|collection|collections|library|libraries|bundle|hub|gallery|marketplace)", re.I)
COLLECTION_DESC = re.compile(
    r"(curated (list|set|collection)|collection of|bundle of|library of|set of|pack of|"
    r"skill (kit|suite|hub|pack|collection)|curated)", re.I)


def is_collection(repo):
    name = repo["name"].lower()
    desc = repo.get("description") or ""
    topics = " ".join(t.lower() for t in (repo.get("topics") or []))
    if COLLECTION_NAME.search(name):
        return True
    if re.search(r"(^|\s)(awesome|collection|curated|bundle|library|hub|marketplace)(\s|$)", topics):
        return True
    if COLLECTION_DESC.search(desc.lower()):
        return True
    return False


def extract_highlights(readme, limit=3):
    """从合集 README 抽取内部子技能亮点：优先列表项链接，其次任意 Markdown 链接。"""
    if not readme:
        return []
    items = re.findall(r"^\s*[-*+]\s+\[([^\]\n]+)\]\(([^)\n]+)\)", readme, re.M)
    if len(items) < 3:
        items = re.findall(r"\[([^\]\n]+)\]\(([^)\n]+)\)", readme)
    out, seen = [], set()
    for text, url in items:
        t = text.strip()
        if not t or len(t) > 60 or url.startswith("#") or t in seen:
            continue
        seen.add(t)
        out.append((t, url))
        if len(out) >= limit:
            break
    return out


def http_json(url, retries=2, backoff=3):
    """GET JSON，处理 403/429 限流与瞬时错误。"""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                wait = int(e.headers.get("Retry-After", backoff * (attempt + 1) * 2))
                wait = min(wait, 60)
                sys.stderr.write(f"  [限流] HTTP {e.code}，等待 {wait}s 后重试\n")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    raise RuntimeError("请求失败（重试次数用尽）")


def gh_search(q, per_page=50):
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "stars", "order": "desc", "per_page": per_page})
    data, headers = http_json(url)
    remaining = headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        sys.stderr.write(f"  [配额] 搜索剩余 {remaining}/min\n")
    return data


def raw_readme(full_name):
    """走 raw 通道抓 README，不消耗搜索/核心 API 配额。"""
    for branch in ("HEAD", "main", "master"):
        url = f"https://raw.githubusercontent.com/{full_name}/{branch}/README.md"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WFGoodSkills"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            continue
    return None


def keep(repo):
    if repo.get("fork") or repo.get("archived"):
        return False
    desc = repo.get("description") or ""
    if not desc.strip():
        return False
    name = repo["name"].lower()
    topics = repo.get("topics") or []
    has_signal = bool(SKILL_SIGNAL.search(name) or SKILL_SIGNAL.search(desc)
                      or any(SKILL_SIGNAL.search(t) for t in topics))
    if not has_signal:
        return False
    if BLACKLIST.search(desc) and not SKILL_SIGNAL.search(desc):
        return False
    return True


def norm(vals):
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return [0.0] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


def fmt_days(days):
    if days >= 730:
        return "＞2年"
    return f"{days}天前"


def score_list(items):
    """对仓库列表做三维评分，原地写入 _s（_days 已由调用方算好）。"""
    S = [math.log10(r["stargazers_count"] + 1) for r in items]
    days = [r["_days"] for r in items]
    R = [math.exp(-d / 180) for d in days]
    D = [math.log10(r["readme_chars"] + 1) for r in items]
    Sn, Rn, Dn = norm(S), norm(R), norm(D)
    for i, r in enumerate(items):
        r["_s"] = 0.5 * Sn[i] + 0.3 * Rn[i] + 0.2 * Dn[i]


def print_board(items, sort_key, with_score=True, extra_col=None):
    header = "| # | 仓库 | 简介 | ★ | 更新 | 容量 |"
    if with_score:
        header += " 分 |"
    if extra_col:
        header += f" {extra_col} |"
    print(header)
    print("|---" * (header.count("|") - 1) + "|")
    order = {"combo": "_s", "stars": "stargazers_count", "active": "_days",
             "detail": "readme_chars", "rising": "_s"}[sort_key]
    reverse = order != "_days"
    for i, r in enumerate(sorted(items, key=lambda r: r[order], reverse=reverse), 1):
        desc = (r.get("description") or "").strip().replace("|", "\\|")
        desc = desc[:60] + ("…" if len(desc) > 60 else "")
        stars = f"{r['stargazers_count']:,}"
        upd = fmt_days(r["_days"])
        cap = f"{r['readme_chars'] / 1000:.1f}KB" if r["readme_chars"] > 0 else "–"
        cells = [str(i), f"[{r['full_name']}]({r['html_url']})", desc, stars, upd, cap]
        if with_score:
            cells.append(f"{r['_s']:.3f}")
        if extra_col:
            cells.append(r.get("_extra", "–"))
        print("| " + " | ".join(cells) + " |")


def run(keyword, top, sort_key, json_path):
    kws = KEYWORD_MAP.get(keyword.lower(), [keyword])[:3]

    # ---------- 召回：标准模板 → 命中不足再补精准/兜底 ----------
    merged, queries_done = {}, 0
    for kw in kws:
        d = gh_search(f'"{kw}" skills fork:false')
        queries_done += 1
        for it in d.get("items", []):
            merged[it["full_name"]] = it
    if len(merged) < 20:
        for kw in kws:
            if len(merged) >= 20 or queries_done >= 6:
                break
            d = gh_search(f'"{kw}" skills topic:claude-skills OR topic:agent-skills fork:false')
            queries_done += 1
            for it in d.get("items", []):
                merged[it["full_name"]] = it
    if len(merged) < 20:
        for kw in kws:
            if len(merged) >= 20 or queries_done >= 6:
                break
            d = gh_search(f'"{kw}" claude-skills fork:false')
            queries_done += 1
            for it in d.get("items", []):
                merged[it["full_name"]] = it

    # ---------- 精筛 + 分流：独立技能 / 大合集 ----------
    cand = [r for r in merged.values() if keep(r)]
    collections = [r for r in cand if is_collection(r)]
    standalone = [r for r in cand if not is_collection(r)]
    standalone.sort(key=lambda r: r["stargazers_count"], reverse=True)
    collections.sort(key=lambda r: r["stargazers_count"], reverse=True)
    sel_ind = standalone[:min(top, len(standalone))]
    sel_coll = collections[:min(5, len(collections))]

    # ---------- 详情：独立榜 Top N + 合集榜 Top 5 抓 README（缓存内容，零额外请求） ----------
    for r in sel_ind + sel_coll:
        rd = raw_readme(r["full_name"])
        r["_readme"] = rd or ""
        r["readme_chars"] = len(rd) if rd else 0
        r["_days"] = (NOW - datetime.datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))).days
        r["_cday"] = (NOW - datetime.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))).days
        time.sleep(0.1)

    # 剔除无效仓库：README 抓取失败且星标过低的（多为空壳/随机生成名仓库）
    def drop_junk(items):
        return [r for r in items if not (r["readme_chars"] == 0 and r["stargazers_count"] < 300)]
    sel_ind = drop_junk(sel_ind)
    sel_coll = drop_junk(sel_coll)

    # 年内热门榜筛选：一年内创建 + 两周内有更新（仅 --sort rising）
    if sort_key == "rising":
        sel_ind = [r for r in sel_ind if r["_cday"] <= RISING_CREATED_DAYS and r["_days"] <= RISING_PUSHED_DAYS]
        sel_coll = [r for r in sel_coll if r["_cday"] <= RISING_CREATED_DAYS and r["_days"] <= RISING_PUSHED_DAYS]
        if not sel_ind:
            print(f"该关键词暂无「一年内创建 + 两周内有更新」的新兴热门独立技能仓库，"
                  f"可换关键词，或用 --sort combo 查看常规综合榜。")
    ind_ok = sum(1 for r in sel_ind if r["readme_chars"] > 0)
    coll_ok = sum(1 for r in sel_coll if r["readme_chars"] > 0)

    # 合集亮点：从 README 抽取内部子技能
    for r in sel_coll:
        if r["readme_chars"] == 0:
            continue
        rd = r["_readme"]
        hl = extract_highlights(rd)
        names = [t for t, _ in hl]
        cnt = min(len(re.findall(r"^\s*[-*+]\s+\[", rd, re.M)), 99)
        parts = []
        if names:
            parts.append("亮点: " + "、".join(names[:3]))
        if cnt > 0:
            parts.append(f"含 ~{cnt} 个子技能条目")
        r["_extra"] = " · ".join(parts) if parts else "–"

    # ---------- 评分（独立榜与合集榜各自归一化） ----------
    score_list(sel_ind)
    score_list(sel_coll)

    # ---------- 输出 ----------
    print(f"召回 {len(merged)} → 独立技能候选 {len(standalone)} → 合集候选 {len(collections)}"
          f" · 独立榜展示 {len(sel_ind)}（README {ind_ok}/{len(sel_ind)}）"
          f" · 合集展示 {len(sel_coll)}（README {coll_ok}/{len(sel_coll)}）"
          f" · 搜索请求 {queries_done} 次 · {NOW.date()}")

    label = {"combo": "综合榜", "stars": "星标榜", "active": "活跃榜",
             "detail": "容量榜", "rising": "年内热门榜"}[sort_key]
    print(f"\n## 独立技能{label} Top {len(sel_ind)}")
    print_board(sel_ind, sort_key)
    if sel_coll:
        print(f"\n## 精选合集{label}（大合集单列对比，避免霸榜）")
        print_board(sel_coll, sort_key, extra_col="内部亮点")
    else:
        print("\n## 精选合集榜（无命中）")

    if json_path:
        def item(r, kind):
            return {
                "kind": kind,
                "full_name": r["full_name"], "html_url": r["html_url"],
                "description": r.get("description") or "",
                "stars": r["stargazers_count"], "days_since_push": r["_days"],
                "days_since_created": r["_cday"],
                "created_at": r.get("created_at", "")[:10],
                "readme_kb": round(r["readme_chars"] / 1000, 1),
                "score_combo": round(r["_s"], 3),
                "highlights": r.get("_extra", ""),
            }
        payload = {
            "keyword": keyword, "fetched_at": NOW.isoformat(),
            "queries": queries_done,
            "total_standalone": len(standalone), "total_collections": len(collections),
            "sort": sort_key, "standalone_readme_ok": ind_ok, "collection_readme_ok": coll_ok,
            "standalone": [item(r, "standalone") for r in sel_ind],
            "collections": [item(r, "collection") for r in sel_coll],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 已写入: {json_path}")


def main():
    ap = argparse.ArgumentParser(description="WFGoodSkills: 搜索并排行 Agent skill 仓库")
    ap.add_argument("keyword", help="分类/功能关键词（中英文均可）")
    ap.add_argument("--top", type=int, default=20, help="独立榜展示条数，默认 20，上限 50")
    ap.add_argument("--sort", choices=["combo", "stars", "active", "detail", "rising"], default="combo",
                    help="combo=综合(默认) stars=星标 active=活跃 detail=容量 rising=年内热门(一年内创建+两周内有更新)")
    ap.add_argument("--json", default=None, help="额外输出结构化 JSON 到该路径")
    args = ap.parse_args()

    if args.top < 1 or args.top > 50:
        sys.exit("--top 必须在 1~50 之间")
    try:
        run(args.keyword, args.top, args.sort, args.json)
    except Exception as e:
        sys.stderr.write(f"执行失败: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
