#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qa_autopsy — AI 客服「问答知识库」尸检

kb_autopsy 量的是「散文档」；但 AI 客服用的是**结构化问答对**，病理完全不同：
文档库怕陈旧，问答库怕 **同一个问题有两个不同答案** —— 那不是"不新鲜"，那是会直接回错话。

本模块专量问答型知识库（QA JSON），五项病理：
  1. 同题异答   同一问题多个答案且互相不一致 → AI 会被问懵 / 答错
  2. 答案复用   一段答案被复制给多个不同问题 → 说明答案没针对性
  3. 无出处     答案没有引用来源 → 合规类知识库的硬伤，答了也站不住
  4. 空壳答案   答案缺失或过短 → 检索到了也答不出东西
  5. 缺字段     缺 slug/question/answer 等必需字段 → 上游数据质量问题

支持两种常见形态（自动识别）：
  A) 簇式：{"cluster": {...}, "atoms": [{slug, question, short_answer, body_md, sources, faqs}, ...]}
  B) 平铺：{"qa": [...]} / {"questions": [...]} / [{question, answer}, ...]

用法：
  python qa_autopsy.py <目录或 json 文件>            # Markdown 报告
  python qa_autopsy.py <目录> --json                 # 结构化
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from kb_autopsy import _jaccard, _ngrams, _visible_len

# ---------------------------------------------------------------- 阈值

QA_DEFAULTS = {
    "q_similar": 0.72,      # 问题相似度高于此值 → 认为是「同一个问题」
    "a_diverge": 0.45,      # 答案相似度低于此值 → 认为是「不一致的答案」
    "answer_min": 40,       # 答案少于该字数视为空壳
    "answer_reuse": 0.90,   # 答案相似度高于此值且问题不同 → 视为复用
}

WEIGHTS_QA = {
    "conflict": 30,     # 同题异答（最致命）
    "no_source": 25,    # 无出处
    "hollow": 15,       # 空壳答案
    "reuse": 15,        # 答案复用
    "missing": 15,      # 缺字段
}

BANDS_QA = {
    "conflict": (0.0, 0.15),
    "no_source": (0.05, 0.50),
    "hollow": (0.02, 0.25),
    "reuse": (0.02, 0.25),
    "missing": (0.0, 0.20),
}

_WS = re.compile(r"[\s\u3000]+")
_PUNCT = re.compile(r"[，。？！、；：,.\?!;:（）()\[\]【】\"'「」『』—\-~·…]+")


def _norm_q(s: str) -> str:
    """问题归一化：去空白、去标点，只留实义字，便于判「是不是同一个问题」。"""
    return _PUNCT.sub("", _WS.sub("", (s or "").strip()))


def _qgrams(s: str):
    return _ngrams(_norm_q(s), 3)


# ---------------------------------------------------------------- 读取

Q_KEYS = ("question", "q", "title")
A_KEYS = ("short_answer", "answer", "a", "body_md", "body")
STRUCT_KEYS = ("slug", "order", "facts", "sources", "faqs", "body_md")


def _mk_record(cluster_slug: str, src: str, a: dict, idx: int) -> dict | None:
    """把一条 dict 变成问答记录。**不像问答对的返回 None。**

    形状校验是必须的：清单/索引类 JSON（如 `_manifest_en.json`，条目形如
    `{"title": "build-vs-buy"}`）会被误当成问答，造出成片的「缺字段+无出处+空壳」
    假阳性（实测：114 条里 34 条是这么来的，占比 30%）。
    收条标准 = 有问题文本，且（有答案文本 或 带问答条目该有的结构字段）。
    """
    q = ""
    for k in Q_KEYS:
        if a.get(k):
            q = str(a[k]).strip()
            break
    has_a = any(a.get(k) for k in A_KEYS)
    has_struct = any(k in a for k in STRUCT_KEYS)
    if not q or not (has_a or has_struct):
        return None

    ans = ""
    for k in ("short_answer", "answer", "a"):
        if a.get(k):
            ans = str(a[k]).strip()
            break
    body = a.get("body_md") or a.get("body") or ""
    faqs = a.get("faqs") or []
    return {
        "src": src,
        "cluster": cluster_slug,
        "slug": (a.get("slug") or a.get("id") or f"#{idx}"),
        "question": q,
        "answer": ans,
        "body": body,
        "sources": a.get("sources") or [],
        "faqs": faqs if isinstance(faqs, list) else [],
        "answer_len": _visible_len(ans) or _visible_len(body[:400]),
        "qg": _qgrams(q),
        "ag": _ngrams(ans, 3),
    }


def _cslug(cluster, default: str = "") -> str:
    """cluster 字段可能是 dict（含 slug），也可能直接是字符串，两种都要认。"""
    if isinstance(cluster, dict):
        return cluster.get("slug") or default
    if isinstance(cluster, str):
        return cluster or default
    return default


def _extract(doc, src: str) -> list[dict]:
    """从任意一种 JSON 形态里抽出问答记录。"""
    out: list[dict] = []

    def walk(items, cluster_slug=""):
        for i, a in enumerate(items):
            if not isinstance(a, dict):
                continue
            # 嵌套簇形态
            if "atoms" in a and isinstance(a["atoms"], list):
                walk(a["atoms"], _cslug(a.get("cluster"), cluster_slug))
                continue
            rec = _mk_record(cluster_slug, src, a, i)
            # 校验「像不像一条问答」：不像的（清单/索引类条目）一律不收。
            # 不收这一步会把 manifest 也当成问答，造出成片的假阳性（实测踩过）。
            if rec is not None:
                out.append(rec)

    if isinstance(doc, list):
        walk(doc)
    elif isinstance(doc, dict):
        if "atoms" in doc:
            walk(doc["atoms"], _cslug(doc.get("cluster")))
        else:
            for key in ("qa", "questions", "items", "data", "atoms", "list"):
                if isinstance(doc.get(key), list):
                    walk(doc[key])
                    break
            else:
                # 当一个 dict 里全是簇，逐个展开
                for k, v in doc.items():
                    if isinstance(v, dict) and isinstance(v.get("atoms"), list):
                        walk(v["atoms"], _cslug(v.get("cluster"), k))
                    elif isinstance(v, list):
                        walk(v, k)
    return out


def load_qa(root: Path) -> tuple[list[dict], list[str]]:
    """收集目录/单文件下的问答 JSON。返回 (记录, 被跳过的文件说明)。"""
    files: list[Path] = []
    if root.is_file():
        files = [root]
    else:
        skip = {".git", "node_modules", ".venv", "venv", "__pycache__",
                "dist", "build", "_site", "public"}
        for p in sorted(root.rglob("*.json")):
            parts = p.relative_to(root).parts
            if any(part in skip or part.startswith(".") for part in parts):
                continue
            files.append(p)

    recs, skipped = [], []
    for p in files:
        # 备份文件不是数据源，扫进来会造出成倍的假重复
        if re.search(r"\.bak\d*|_bak|backup|_before_", p.name, re.I):
            skipped.append(f"{p.name}（备份文件）")
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            skipped.append(f"{p.name}（不是合法 JSON）")
            continue
        got = _extract(doc, str(p.relative_to(root)) if root.is_dir() else p.name)
        if not got:
            skipped.append(f"{p.name}（没有问答结构）")
            continue
        recs.extend(got)
    return recs, skipped


# ---------------------------------------------------------------- 分析

def _band(v, lo, hi):
    if hi <= lo:
        return 1.0 if v >= hi else 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def analyze_qa(root: Path, cfg: dict | None = None) -> dict:
    cfg = {**QA_DEFAULTS, **(cfg or {})}
    recs, skipped = load_qa(root)
    n = len(recs)
    if n == 0:
        return {"root": str(root), "atom_count": 0,
                "error": "没有找到问答结构（需要 {question, answer} 或 {cluster, atoms} 形态的 JSON）",
                "skipped_files": skipped}
    MIN = 5
    if n < MIN:
        return {"root": str(root), "atom_count": n,
                "error": f"问答量太小（{n} 条，少于 {MIN} 条），给不出有意义的判断。",
                "skipped_files": skipped}

    # ---- 1. 同题异答：问题像同一个，答案却不像同一件事
    conflicts = []
    for i in range(n):
        for j in range(i + 1, n):
            ri, rj = recs[i], recs[j]
            if not ri["qg"] or not rj["qg"]:
                continue
            qs = _jaccard(ri["qg"], rj["qg"])
            if qs < cfg["q_similar"]:
                continue
            asim = _jaccard(ri["ag"], rj["ag"]) if (ri["ag"] and rj["ag"]) else 0.0
            if asim < cfg["a_diverge"]:
                conflicts.append({
                    "q_similarity": round(qs, 3), "a_similarity": round(asim, 3),
                    "a": {"src": ri["src"], "slug": ri["slug"], "question": ri["question"],
                          "answer": ri["answer"][:90]},
                    "b": {"src": rj["src"], "slug": rj["slug"], "question": rj["question"],
                          "answer": rj["answer"][:90]},
                })
    conflict_docs = {c["a"]["slug"] for c in conflicts} | {c["b"]["slug"] for c in conflicts}
    conflict_ratio = len(conflict_docs) / n

    # ---- 2. 答案复用：不同问题用了几乎一样的答案
    reuse = []
    for i in range(n):
        for j in range(i + 1, n):
            ri, rj = recs[i], recs[j]
            if not ri["ag"] or not rj["ag"]:
                continue
            if _jaccard(ri["qg"], rj["qg"]) >= cfg["q_similar"]:
                continue          # 同题不算复用，算上面那项
            a_sim = _jaccard(ri["ag"], rj["ag"])
            if a_sim >= cfg["answer_reuse"]:
                reuse.append({"a_similarity": round(a_sim, 3), "a": ri["slug"], "b": rj["slug"],
                              "qa": ri["question"][:50], "qb": rj["question"][:50]})
    reuse_docs = {r["a"] for r in reuse} | {r["b"] for r in reuse}
    reuse_ratio = len(reuse_docs) / n

    # ---- 3. 无出处：合规/事实类答案没有来源 = 答了也站不住
    no_src = [r["slug"] for r in recs if not r["sources"]]
    no_src_ratio = len(no_src) / n

    # ---- 4. 空壳答案
    hollow = [r["slug"] for r in recs if r["answer_len"] < cfg["answer_min"]]
    hollow_ratio = len(hollow) / n

    # ---- 5. 缺字段
    missing = []
    for r in recs:
        lack = [k for k in ("question", "answer", "slug") if not r[k]]
        if lack:
            missing.append({"slug": r["slug"] or "(无 slug)", "missing": lack})
    missing_ratio = len(missing) / n

    parts = {
        "conflict": _band(conflict_ratio, *BANDS_QA["conflict"]),
        "no_source": _band(no_src_ratio, *BANDS_QA["no_source"]),
        "hollow": _band(hollow_ratio, *BANDS_QA["hollow"]),
        "reuse": _band(reuse_ratio, *BANDS_QA["reuse"]),
        "missing": _band(missing_ratio, *BANDS_QA["missing"]),
    }
    score = round(sum(parts[k] * WEIGHTS_QA[k] for k in WEIGHTS_QA), 1)

    return {
        "root": str(root),
        "atom_count": n,
        "file_count": len({r["src"] for r in recs}),
        "skipped_files": skipped,
        "qa_zombie_index": score,
        "verdict": _qa_verdict(score),
        "metrics": {
            "conflict": {"ratio": round(conflict_ratio, 4), "count": len(conflict_docs),
                         "score": round(parts["conflict"], 3), "pairs": conflicts[:10]},
            "no_source": {"ratio": round(no_src_ratio, 4), "count": len(no_src),
                          "score": round(parts["no_source"], 3), "examples": no_src[:10]},
            "hollow": {"ratio": round(hollow_ratio, 4), "count": len(hollow),
                       "score": round(parts["hollow"], 3), "examples": hollow[:10]},
            "reuse": {"ratio": round(reuse_ratio, 4), "count": len(reuse_docs),
                      "score": round(parts["reuse"], 3), "pairs": reuse[:10]},
            "missing": {"ratio": round(missing_ratio, 4), "count": len(missing),
                        "score": round(parts["missing"], 3), "examples": missing[:10]},
        },
        "weights": WEIGHTS_QA,
    }


def _qa_verdict(z: float) -> dict:
    if z < 25:
        return {"level": "健康", "emoji": "🟢",
                "line": "问答库结构干净。保持一题一答、有据可依。"}
    if z < 50:
        return {"level": "亚健康", "emoji": "🟡",
                "line": "已经有隐患。重点看下面得分最高的那一项，现在补成本最低。"}
    if z < 75:
        return {"level": "危险", "emoji": "🟠",
                "line": "问答库开始互相打架。同题异答会让 AI 客服稳定地答错话。"}
    return {"level": "失控", "emoji": "🔴",
            "line": "问答库自己就矛盾。别急着上线——先合并同题、补齐出处。"}


def render_qa_markdown(r: dict) -> str:
    if r.get("error"):
        return f"# AI 客服问答库尸检\n\n❌ {r['error']}\n"
    m = r["metrics"]
    v = r["verdict"]
    L = []
    A = L.append
    A("# AI 客服问答库尸检报告\n")
    A(f"**目录**：`{r['root']}`  ")
    A(f"**问答条数**：{r['atom_count']}（来自 {r['file_count']} 个文件）\n")
    A(f"## {v['emoji']} 问答库危险指数 {r['qa_zombie_index']} / 100 —— {v['level']}\n")
    A(f"> {v['line']}\n")
    A("| 病理 | 权重 | 实测 | 得分 |")
    A("|---|---|---|---|")
    rows = [
        ("conflict", "同题异答", f"{m['conflict']['ratio']*100:.1f}%（{m['conflict']['count']} 条卷入）"),
        ("no_source", "无出处", f"{m['no_source']['ratio']*100:.1f}%（{m['no_source']['count']} 条无来源）"),
        ("hollow", "空壳答案", f"{m['hollow']['ratio']*100:.1f}%（{m['hollow']['count']} 条过短）"),
        ("reuse", "答案复用", f"{m['reuse']['ratio']*100:.1f}%（{m['reuse']['count']} 条答案被复用）"),
        ("missing", "缺字段", f"{m['missing']['ratio']*100:.1f}%（{m['missing']['count']} 条缺必需字段）"),
    ]
    for key, label, desc in rows:
        sc = m[key]["score"]
        bar = "█" * int(round(sc * 10)) + "░" * (10 - int(round(sc * 10)))
        A(f"| {label} | {WEIGHTS_QA[key]} | {desc} | `{bar}` {sc*100:.0f}% |")
    A("")
    if m["conflict"]["pairs"]:
        A("### 🔴 同题异答（AI 客服会在这里稳定答错）\n")
        for c in m["conflict"]["pairs"][:5]:
            A(f"**问法重合 {c['q_similarity']*100:.0f}%，答案只重合 {c['a_similarity']*100:.0f}%**")
            A(f"- `{c['a']['slug']}`「{c['a']['question'][:60]}」→ {c['a']['answer'][:70]}…")
            A(f"- `{c['b']['slug']}`「{c['b']['question'][:60]}」→ {c['b']['answer'][:70]}…")
            A("")
    if m["no_source"]["examples"]:
        A("### 无出处的条目（合规库的硬伤）\n")
        for e in m["no_source"]["examples"][:8]:
            A(f"- `{e}`")
        A("")
    if m["reuse"]["pairs"]:
        A("### 答案复用（一段答案发给多个问题）\n")
        for p in m["reuse"]["pairs"][:5]:
            A(f"- `{p['a']}` ↔ `{p['b']}`（答案重合 {p['a_similarity']*100:.0f}%）")
        A("")
    if r.get("skipped_files"):
        A("### 已跳过的文件\n")
        for s in r["skipped_files"][:8]:
            A(f"- {s}")
        A("")
    A("---\n")
    A("**最该先修的一件事**：把同题异答合并成一条。 ")
    A("同一个问题有两个答案，AI 客服答哪个都是错——这是问答库唯一无法容忍的病。\n")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qa_autopsy",
                                 description="AI 客服问答库尸检：同题异答 / 无出处 / 空壳 / 复用 / 缺字段")
    ap.add_argument("root")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--q-similar", type=float, default=QA_DEFAULTS["q_similar"])
    ap.add_argument("--a-diverge", type=float, default=QA_DEFAULTS["a_diverge"])
    a = ap.parse_args(argv)
    root = Path(a.root)
    if not root.exists():
        print(f"❌ 路径不存在：{root}", file=sys.stderr)
        return 2
    cfg = {**QA_DEFAULTS, "q_similar": a.q_similar, "a_diverge": a.a_diverge}
    r = analyze_qa(root, cfg)
    out = json.dumps(r, ensure_ascii=False, indent=2) if a.json else render_qa_markdown(r)
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
        print(f"✅ 报告已写入 {a.out}")
    else:
        print(out)
    return 0 if not r.get("error") else 3


if __name__ == "__main__":
    raise SystemExit(main())
