#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb_autopsy — 企业知识库「尸检」工具

输入一个 Markdown / 纯文本知识库目录，输出一份体检报告：僵尸指数 + 五项病理指标。
核心命题：知识库不是买来的，是养出来的。没人负责、没有更新节奏、没有人在看，
再好的系统也会变成僵尸。这个工具把「有没有在养」变成可核验的数字。

设计约束：
  · 零外部依赖（纯标准库），任何人 clone 下来就能跑 —— 这是能被自发传播的前提
  · 中文不依赖分词库：用「字符 n-gram Jaccard」度量近重复，字符级对中文反而更稳
  · 只读，绝不修改用户的任何文件

用法：
  python kb_autopsy.py <知识库目录>              # 打印 Markdown 报告
  python kb_autopsy.py <知识库目录> --json       # 输出 JSON
  python kb_autopsy.py <知识库目录> --out r.md   # 报告写入文件
  python kb_autopsy.py <知识库目录> --stale-days 180
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------- 可调阈值

DEFAULTS = {
    # 文档被认为「陈旧」的天数（超过则计入不新鲜）
    "stale_days": 180,
    # 少于该字数的文档视为「空壳」
    "hollow_chars": 200,
    # 字符 n-gram 重合度高于该值视为「近重复」
    "dup_threshold": 0.75,
    # n-gram 的 n（中文用 3 比较稳）
    "ngram": 3,
}

TEXT_EXT = {".md", ".markdown", ".txt", ".text", ".rst"}

# 僵尸指数五项指标权重（合计 100）
WEIGHTS = {
    "staleness": 30,   # 陈旧度：多久没人更新
    "orphan": 25,      # 孤儿率：没人引用、进不去的孤岛文档
    "hollow": 15,      # 空壳率：只有标题没有内容
    "duplicate": 15,   # 重复率：同一件事写了多遍，AI 会答混
    "unstructured": 15,  # 无结构率：没标题没分节，检索效果差
}

# 每一项的「及格线」：低于此值的原始比例得 0 分，高于上限得满分
BANDS = {
    "orphan": (0.05, 0.50),
    "hollow": (0.02, 0.30),
    "duplicate": (0.02, 0.30),
    "unstructured": (0.05, 0.50),
}

# ---------------------------------------------------------------- 读取

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_DATE_IN_NAME = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")
_DATE_IN_FM = re.compile(r"^\s*(?:date|created|updated|创建时间|更新时间)\s*[:：]\s*(\S+)", re.M)


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def _visible_len(text: str) -> int:
    """去掉 Markdown 标记、代码块、链接 URL 后，正文的实际字符数。"""
    t = re.sub(r"```.*?```", "", text, flags=re.S)          # 代码块
    t = re.sub(r"`[^`]*`", "", t)                            # 行内代码
    t = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", t)              # 链接/图片
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)      # 标题标记
    t = re.sub(r"^\s*[-*+>]\s+", "", t, flags=re.M)          # 列表/引用
    t = re.sub(r"[|#*_~>`-]", "", t)                         # 其余杂符
    return len(re.sub(r"\s", "", t))


def _ngrams(text: str, n: int) -> set[str]:
    """字符级 n-gram 集合。中文不分词也稳，且天然忽略语序噪声。"""
    t = re.sub(r"\s+", "", text)
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


class Doc:
    __slots__ = ("path", "rel", "text", "body", "chars", "mtime", "date_guess",
                 "headings", "outlinks", "grams", "mtime_source", "_now")

    def __init__(self, path: Path, root: Path, now_ts: float):
        self.path = path
        self.rel = str(path.relative_to(root)).replace("\\", "/")
        raw = path.read_text(encoding="utf-8", errors="replace")
        self.text = raw
        self.body = _strip_frontmatter(raw)
        self.chars = _visible_len(self.body)
        self.headings = re.findall(r"^\s{0,3}#{1,6}\s+(.+)$", self.body, re.M)
        self.outlinks = _extract_links(self.body)
        self.grams = _ngrams(self.body, DEFAULTS["ngram"])

        # 时间优先级：frontmatter 日期 > 文件名日期 > 文件 mtime
        self.date_guess, self.mtime_source = None, "mtime"
        m = _DATE_IN_FM.search(raw)
        if m:
            d = _parse_date(m.group(1))
            if d:
                self.date_guess, self.mtime_source = d, "frontmatter"
        if self.date_guess is None:
            m = _DATE_IN_NAME.search(path.name)
            if m:
                d = _parse_date("-".join(m.groups()))
                if d:
                    self.date_guess, self.mtime_source = d, "filename"
        self.mtime = self.date_guess.timestamp() if self.date_guess else path.stat().st_mtime
        self._now = now_ts

    @property
    def age_days(self) -> float:
        return max(0.0, (self._now - self.mtime) / 86400.0)


def _parse_date(s: str):
    s = s.strip().strip("'\"")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return _dt.datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    m = re.match(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


_LINK_MD = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_LINK_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def _extract_links(body: str) -> set[str]:
    """抽出文档内部指向其他文档的链接目标（本地链接才算）。"""
    out = set()
    for m in _LINK_MD.finditer(body):
        t = m.group(1).split("#")[0].strip()
        if t and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", t) and not t.startswith("mailto:"):
            out.add(t.replace("\\", "/").lstrip("./"))
    for m in _LINK_WIKI.finditer(body):
        t = m.group(1).split("#")[0].strip()
        if t:
            out.add(t)
    return out


# 已知的构建产物 / 依赖 / 站点基础设施目录：它们不是「知识库文档」
SKIP_DIR_PARTS = {
    ".git", "node_modules", ".obsidian", "__pycache__", ".venv", "venv", ".trash",
    "dist", "build", "_site", "public", "output", "out", "_aeo_out", "_aeo_stage",
}

# 站点协议 / 校验 / 密钥类文件：扫进来会制造大量假重复与假空壳
INFRA_FILE = re.compile(
    r"^(?:security|robots|humans|browserconfig|manifest|ads)\.(?:txt|xml|json)$"
    r"|^sitemap[\w.-]*\.(?:txt|xml)$"
    r"|^indexnow[\w.-]*\.(?:txt|json)$"
    r"|^llms(?:-full)?\.txt$"
    r"|^[\w.-]*verification[\w.-]*\.(?:txt|html|json)$"
    r"|^[0-9a-f]{16,}\.txt$"          # 各类校验文件用的哈希名
    r"|^[\w.-]+\.key(?:\.txt)?$",
    re.I,
)


def load(root: Path, skip_dirs=None, skip_hidden: bool = True) -> list[Doc]:
    """收集目录下的文本文档。

    必须滤掉两类「根本不是文档的东西」，否则报告会全盘失真（实测踩过）：
      ① 构建产物/依赖目录（dist / _aeo_out / public …）——同一份内容存在两三份，造出假重复
      ② 站点协议与校验文件（security.txt / indexnow 哈希名 / llms.txt …）——被算成空壳
    对一份静态站点构建目录跑过，得出「濒危 56.2」：孤儿 98%、重复 48%、空壳 13%，
    全部来自上面这两类噪声。**尺子量错对象，比没有尺子更误导。**
    """
    skip = set(SKIP_DIR_PARTS if skip_dirs is None else skip_dirs)
    now = _dt.datetime.now().timestamp()
    docs: list[Doc] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
            continue
        parts = p.relative_to(root).parts
        if any(part in skip for part in parts):
            continue
        if skip_hidden and any(part.startswith(".") for part in parts):
            continue
        if INFRA_FILE.match(p.name):
            continue
        try:
            docs.append(Doc(p, root, now))
        except OSError:
            continue
    return docs


# ---------------------------------------------------------------- 五项指标

def _band_score(value: float, lo: float, hi: float) -> float:
    """value <= lo → 0 分（健康）；value >= hi → 1 分（满病）。线性插值。"""
    if hi <= lo:
        return 1.0 if value >= hi else 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def analyze(root: Path, cfg: dict, include_all: bool = False) -> dict:
    docs = load(root, skip_dirs=set() if include_all else None,
                skip_hidden=not include_all)
    n = len(docs)
    if n == 0:
        return {"root": str(root), "doc_count": 0, "error": "目录下没有找到 Markdown/文本文件"}

    # ---- 语料规模自适应（关键，别跳过）----
    # 小语料天然「零交叉引用」「难有重复」。若照常计入，2 篇的库恒得 25 分被判「亚健康」。
    # 实测：2 篇的库 40.6 分、3 篇 41.9 分、4 篇 25.0 分，**病根全部是孤儿项 100%**——纯噪声。
    # 而小微企业恰恰就是小知识库，正是本工具的目标客群，绝不能在这个尺寸上胡判。
    # 故：小语料裁掉不适用的指标，权重按比例摊回其余项，并在报告里写明裁了什么。
    MIN_DOCS = 5
    if n < MIN_DOCS:
        return {
            "root": str(root), "doc_count": n,
            "error": f"语料太小（{n} 篇，少于 {MIN_DOCS} 篇），给不出有意义的僵尸指数。"
                     f"知识库体检至少要 {MIN_DOCS} 篇文档才谈得上「有没有在养」。",
        }
    active = dict(WEIGHTS)
    skipped = {}
    if n < 15:
        skipped["orphan"] = active.pop("orphan")
    if n < 10:
        skipped["duplicate"] = active.pop("duplicate")
    if skipped:  # 把裁掉的权重按剩余项的比例摊回去，总分仍是 100
        scale = 100.0 / sum(active.values())
        active = {k: round(v * scale, 4) for k, v in active.items()}

    # ---- 1. 陈旧度：用「文档年龄中位数」而非平均值，避免个别老文件拉偏
    ages = sorted(d.age_days for d in docs)
    median_age = ages[n // 2]
    stale = sum(1 for d in docs if d.age_days > cfg["stale_days"])
    stale_ratio = stale / n
    # 中位数年龄映射：0 天 → 0 分；2 个 stale_days → 满分
    staleness_score = max(0.0, min(1.0, median_age / (2 * cfg["stale_days"])))

    # ---- 2. 孤儿率：没有任何其他文档链接指向它，也没有被文件名提及
    stem_index = defaultdict(list)
    for d in docs:
        stem_index[d.path.stem.lower()].append(d.rel)
        stem_index[d.path.name.lower()].append(d.rel)
    inbound = Counter()
    for d in docs:
        for link in d.outlinks:
            key = link.lower()
            target = stem_index.get(Path(key).stem.lower())
            if not target:
                target = stem_index.get(key)
            for t in (target or []):
                if t != d.rel:
                    inbound[t] += 1
    orphans = [d.rel for d in docs if inbound.get(d.rel, 0) == 0]
    orphan_ratio = len(orphans) / n

    # ---- 3. 空壳率
    hollow = [d.rel for d in docs if d.chars < cfg["hollow_chars"]]
    hollow_ratio = len(hollow) / n

    # ---- 4. 重复率：字符 n-gram Jaccard 近重复对
    dup_pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            di, dj = docs[i], docs[j]
            # 长度差太大先剪枝，省时间
            if min(di.chars, dj.chars) == 0:
                continue
            if max(di.chars, dj.chars) / max(1, min(di.chars, dj.chars)) > 3:
                continue
            s = _jaccard(di.grams, dj.grams)
            if s >= cfg["dup_threshold"]:
                dup_pairs.append({"a": di.rel, "b": dj.rel, "similarity": round(s, 3)})
    dup_docs = {p["a"] for p in dup_pairs} | {p["b"] for p in dup_pairs}
    dup_ratio = len(dup_docs) / n

    # ---- 5. 无结构率：没有标题也没有分节
    unstructured = [d.rel for d in docs if not d.headings and d.chars >= cfg["hollow_chars"]]
    unstructured_ratio = len(unstructured) / n

    # ---- 合成僵尸指数
    parts = {
        "staleness": staleness_score,
        "orphan": _band_score(orphan_ratio, *BANDS["orphan"]),
        "hollow": _band_score(hollow_ratio, *BANDS["hollow"]),
        "duplicate": _band_score(dup_ratio, *BANDS["duplicate"]),
        "unstructured": _band_score(unstructured_ratio, *BANDS["unstructured"]),
    }
    zombie = round(sum(parts[k] * active[k] for k in active), 1)

    return {
        "root": str(root),
        "doc_count": n,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "zombie_index": zombie,
        "verdict": _verdict(zombie),
        "metrics": {
            "staleness": {
                "median_age_days": round(median_age, 1),
                "oldest_days": round(ages[-1], 1),
                "newest_days": round(ages[0], 1),
                "stale_ratio": round(stale_ratio, 4),
                "stale_count": stale,
                "stale_days_threshold": cfg["stale_days"],
                "score": round(parts["staleness"], 3),
            },
            "orphan": {
                "ratio": round(orphan_ratio, 4), "count": len(orphans),
                "score": round(parts["orphan"], 3), "examples": orphans[:8],
            },
            "hollow": {
                "ratio": round(hollow_ratio, 4), "count": len(hollow),
                "score": round(parts["hollow"], 3), "examples": hollow[:8],
            },
            "duplicate": {
                "ratio": round(dup_ratio, 4), "count": len(dup_docs),
                "score": round(parts["duplicate"], 3),
                "pairs": sorted(dup_pairs, key=lambda x: -x["similarity"])[:8],
            },
            "unstructured": {
                "ratio": round(unstructured_ratio, 4), "count": len(unstructured),
                "score": round(parts["unstructured"], 3), "examples": unstructured[:8],
            },
        },
        "weights": WEIGHTS,
        "active_weights": active,
        "skipped_metrics": skipped,
    }


def _verdict(z: float) -> dict:
    if z < 25:
        return {"level": "健康", "emoji": "🟢",
                "line": "这套知识库有人在养。保持更新节奏和负责人不变。"}
    if z < 50:
        return {"level": "亚健康", "emoji": "🟡",
                "line": "已经开始失养。现在补还来得及，重点看下面得分最高的那一项。"}
    if z < 75:
        return {"level": "濒危", "emoji": "🟠",
                "line": "正在变成僵尸。通常缺的不是工具，是一个明确的人。"}
    return {"level": "僵尸", "emoji": "🔴",
            "line": "已经死了。继续往里投工具解决不了问题——先回答「谁负责」。"}


# ---------------------------------------------------------------- 报告

def render_markdown(r: dict) -> str:
    if r.get("error"):
        return f"# 知识库尸检\n\n❌ {r['error']}\n"
    m = r["metrics"]
    v = r["verdict"]
    L = []
    A = L.append
    A("# 知识库尸检报告\n")
    A(f"**目录**：`{r['root']}`  ")
    A(f"**文档数**：{r['doc_count']}  ")
    A(f"**生成时间**：{r['generated_at']}\n")
    A(f"## {v['emoji']} 僵尸指数 {r['zombie_index']} / 100 —— {v['level']}\n")
    A(f"> {v['line']}\n")
    A("| 指标 | 权重 | 实测 | 得分 |")
    A("|---|---|---|---|")
    aw = r.get("active_weights", WEIGHTS)
    skipped = r.get("skipped_metrics", {})
    order = [
        ("staleness", "陈旧度", f"中位 {m['staleness']['median_age_days']} 天未更新"),
        ("orphan", "孤儿率", f"{m['orphan']['ratio']*100:.1f}%（{m['orphan']['count']} 篇无人引用）"),
        ("hollow", "空壳率", f"{m['hollow']['ratio']*100:.1f}%（{m['hollow']['count']} 篇少于 {DEFAULTS['hollow_chars']} 字）"),
        ("duplicate", "重复率", f"{m['duplicate']['ratio']*100:.1f}%（{m['duplicate']['count']} 篇近重复）"),
        ("unstructured", "无结构率", f"{m['unstructured']['ratio']*100:.1f}%（{m['unstructured']['count']} 篇无标题）"),
    ]
    for key, label, desc in order:
        if key in skipped:
            A(f"| ~~{label}~~ | 不计 | 语料偏小，此指标噪声大于信号，已剔除 | — |")
            continue
        sc = m[key]["score"]
        bar = "█" * int(sc * 10) + "░" * (10 - int(sc * 10))
        A(f"| {label} | {aw.get(key, WEIGHTS[key]):g} | {desc} | `{bar}` {sc*100:.0f}% |")
    A("")
    if skipped:
        names = {"orphan": "孤儿率", "duplicate": "重复率"}
        A(f"> ℹ️ 本次共 {r['doc_count']} 篇文档，语料偏小，已剔除 "
          f"**{'、'.join(names[k] for k in skipped)}**（小语料下这两项的噪声大于信号），"
          f"权重已按比例摊回其余指标。**别拿小库的这两个数说事。**\n")
    if m["duplicate"]["pairs"]:
        A("### ⚠️ 近重复文档（AI 客服最容易答混的地方）\n")
        for p in m["duplicate"]["pairs"][:5]:
            A(f"- `{p['a']}` ↔ `{p['b']}`（重合 {p['similarity']*100:.0f}%）")
        A("")
    if m["hollow"]["examples"]:
        A("### 空壳文档\n")
        for e in m["hollow"]["examples"][:6]:
            A(f"- `{e}`")
        A("")
    if m["orphan"]["examples"]:
        A("### 孤儿文档（没有任何其他文档引用）\n")
        for e in m["orphan"]["examples"][:6]:
            A(f"- `{e}`")
        A("")
    A("---\n")
    A("**下一步只看一件事**：得分最高的那一项。 ")
    A("数字再难看也不是问题，没有负责人、没有更新节奏才是问题。\n")
    return "\n".join(L)


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="kb_autopsy", description="企业知识库尸检：给出僵尸指数与五项病理指标")
    ap.add_argument("root", help="知识库目录")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", help="报告写入文件")
    ap.add_argument("--stale-days", type=int, default=DEFAULTS["stale_days"],
                    help=f"陈旧阈值天数（默认 {DEFAULTS['stale_days']}）")
    ap.add_argument("--dup-threshold", type=float, default=DEFAULTS["dup_threshold"],
                    help=f"近重复判定阈值（默认 {DEFAULTS['dup_threshold']}）")
    ap.add_argument("--hollow-chars", type=int, default=DEFAULTS["hollow_chars"],
                    help=f"空壳字数阈值（默认 {DEFAULTS['hollow_chars']}）")
    ap.add_argument("--all", action="store_true",
                    help="不过滤构建目录/隐藏文件/站点协议文件（默认过滤，避免假阳性）")
    a = ap.parse_args(argv)

    root = Path(a.root)
    if not root.is_dir():
        print(f"❌ 不是目录：{root}", file=sys.stderr)
        return 2
    cfg = dict(DEFAULTS)
    cfg.update(stale_days=a.stale_days, dup_threshold=a.dup_threshold,
               hollow_chars=a.hollow_chars)

    r = analyze(root, cfg, include_all=a.all)
    out = json.dumps(r, ensure_ascii=False, indent=2) if a.json else render_markdown(r)
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
        print(f"✅ 报告已写入 {a.out}")
    else:
        print(out)
    return 0 if not r.get("error") else 3


if __name__ == "__main__":
    raise SystemExit(main())
