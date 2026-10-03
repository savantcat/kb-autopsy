#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb_autopsy 自检：造两个已知答案的语料，看尺子有没有指反。

对照组 A「健康库」：全部新鲜、互相引用、内容充实、每个都有标题
对照组 B「僵尸库」：全部陈旧、零引用、空壳、互为重复、没有标题

如果 A 的僵尸指数不显著低于 B，这把尺子就是坏的 —— 必须先修尺子，不许拿去量客户。
"""
import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kb_autopsy as K  # noqa: E402

NOW = dt.datetime.now()

# 注意：kb_autopsy 有 MIN_DOCS=5 的下限（少于 5 篇拒绝出分），
# 所以对照组必须 ≥5 篇，否则测不到真实路径。
P_A = [  # 健康库：5 篇，互相引用，内容充实，各有标题
    ("index.md", "# 总览\n\n本文汇总。[客服手册](manual.md)、[部署说明](deploy.md)、[常见问题](faq.md)、[术语表](glossary.md) 见链接。\n" + "知识库需要有人负责维护更新。" * 30),
    ("manual.md", "# 客服手册\n\n转人工规则见 [总览](index.md) 与 [部署说明](deploy.md)。\n" + "转人工的判定标准要写清楚。" * 30),
    ("deploy.md", "# 部署说明\n\n安装步骤见 [总览](index.md)。\n" + "部署完成后要指定负责人。" * 30),
    ("faq.md", "# 常见问题\n\n参考 [客服手册](manual.md)。\n" + "常见问题要定期更新答案。" * 30),
    ("glossary.md", "# 术语表\n\n词汇定义见 [总览](index.md)。\n" + "术语必须统一口径。" * 30),
]

P_B = [  # 僵尸库：5 篇，零引用，空壳，两份逐字相同，全无标题
    ("a.md", "就这些。"),
    ("b.md", ""),
    ("c.md", "转人工的判定标准要写清楚。" * 30),   # 与 d 近重复
    ("d.md", "转人工的判定标准要写清楚。" * 30),
    ("e.md", ""),
]


def build(tmp: Path, items, days_old: int, link_up: bool):
    d = tmp
    d.mkdir(parents=True, exist_ok=True)
    for name, body in items:
        p = d / name
        p.write_text(body, encoding="utf-8")
        if days_old:
            ts = (NOW - dt.timedelta(days=days_old)).timestamp()
            os.utime(p, (ts, ts))
    return d


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kbautopsy_"))
    try:
        a = build(tmp / "healthy", P_A, days_old=3, link_up=True)
        b = build(tmp / "zombie", P_B, days_old=900, link_up=False)

        ra = K.analyze(a, K.DEFAULTS)
        rb = K.analyze(b, K.DEFAULTS)
        za, zb = ra["zombie_index"], rb["zombie_index"]

        print(f"A 健康库  僵尸指数 = {za:5.1f}  ({ra['verdict']['level']})")
        print(f"B 僵尸库  僵尸指数 = {zb:5.1f}  ({rb['verdict']['level']})")
        print(f"差值 = {zb - za:+.1f}")
        print()
        print("A 各项得分：", {k: round(v["score"], 3) for k, v in ra["metrics"].items()})
        print("B 各项得分：", {k: round(v["score"], 3) for k, v in rb["metrics"].items()})
        print()

        ok = True
        if not (zb - za >= 40):
            print("❌ 分离度不足：健康库与僵尸库的指数差距 < 40，尺子不够灵敏或指反了")
            ok = False
        else:
            print(f"✅ 分离度 OK（差 {zb - za:.1f} > 40）")

        # 逐项方向检查：B 的每一项都不该低于 A
        wrong = [k for k in K.WEIGHTS if rb["metrics"][k]["score"] < ra["metrics"][k]["score"] - 1e-9]
        if wrong:
            print(f"❌ 这些指标方向反了（僵尸库得分反而更低）：{wrong}")
            ok = False
        else:
            print("✅ 五项指标方向全部正确（僵尸库每项都 ≥ 健康库）")

        # 具体事实检查
        if rb["metrics"]["hollow"]["count"] < 2:
            print("❌ 空壳检测漏了（应识别出至少 2 篇）"); ok = False
        else:
            print(f"✅ 空壳检测：B 识别 {rb['metrics']['hollow']['count']} 篇")
        if not rb["metrics"]["duplicate"]["pairs"]:
            print("❌ 近重复检测漏了（c/d 是逐字相同）"); ok = False
        else:
            top = rb["metrics"]["duplicate"]["pairs"][0]
            print(f"✅ 近重复检测：{top['a']} ↔ {top['b']} 重合 {top['similarity']*100:.0f}%")
        if rb["metrics"]["staleness"]["median_age_days"] < 800:
            print("❌ 陈旧度算错"); ok = False
        else:
            print(f"✅ 陈旧度：B 中位 {rb['metrics']['staleness']['median_age_days']} 天")
        if ra["metrics"]["orphan"]["count"] != 0:
            print(f"❌ 健康库不该有孤儿，却报了 {ra['metrics']['orphan']['count']} 篇"); ok = False
        else:
            print("✅ 引用图正确：健康库 0 孤儿")
        if ra["metrics"]["unstructured"]["count"] != 0:
            print(f"❌ 健康库不该有无标题文档，却报了 {ra['metrics']['unstructured']['count']} 篇"); ok = False
        else:
            print("✅ 结构检测正确：健康库全部有标题")

        # 小语料下限：少于 MIN_DOCS 必须拒绝出分，而不是硬凑一个数出来
        tiny = build(tmp / "tiny", P_A[:2], days_old=1, link_up=True)
        rt = K.analyze(tiny, K.DEFAULTS)
        if rt.get("error"):
            print(f"✅ 小语料下限生效：2 篇被拒绝出分")
        else:
            print(f"❌ 小语料下限失效：2 篇竟给出 {rt['zombie_index']} 分（正是之前误判的根源）")
            ok = False

        print()
        print("自检结果：" + ("✅ 全部通过，尺子可用" if ok else "❌ 未通过，先修再上线"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
