#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb-autopsy MCP 冒烟测试：真的起服务、真的握手、真的调工具。
不测「代码看起来对不对」，只测「Agent 能不能用上」。
"""
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
SERVER = ROOT / "server.py"
SAMPLE = r"D:/Hermeswork/less-ai-tone"


async def main() -> int:
    params = StdioServerParameters(command=PY, args=[str(SERVER)], cwd=str(ROOT))
    ok = True
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            print("✅ MCP 握手成功")

            tools = (await s.list_tools()).tools
            names = [t.name for t in tools]
            print(f"✅ 工具列表：{names}")
            for want in ("autopsy", "autopsy_json", "zombie_index", "qa_autopsy"):
                if want not in names:
                    print(f"❌ 缺少工具：{want}")
                    ok = False

            # 四个 annotation hint 必须全部声明（缺任一个会被 OpenAI 目录拒收）
            print("\n--- annotations 检查（目录收录硬门槛）---")
            for t in tools:
                ann = getattr(t, "annotations", None)
                # ⚠️ mcp 2.x 的 SDK 对象字段是**蛇形**（read_only_hint），协议 JSON 里才是驼峰。
                #    用驼峰去 getattr 会全取到 None，把「齐全」误报成「缺失」（踩过）。
                got = {}
                for proto, py in (("readOnlyHint", "read_only_hint"),
                                  ("destructiveHint", "destructive_hint"),
                                  ("idempotentHint", "idempotent_hint"),
                                  ("openWorldHint", "open_world_hint")):
                    v = getattr(ann, py, None)
                    if v is None:
                        v = getattr(ann, proto, None)
                    got[proto] = v
                missing = [k for k, v in got.items() if v is None]
                if missing:
                    print(f"  ❌ {t.name} 缺 hint：{missing}")
                    ok = False
                else:
                    print(f"  ✅ {t.name} 四个 hint 齐全 {got}")

            print("\n--- 调用 zombie_index ---")
            r = await s.call_tool("zombie_index", {"path": SAMPLE})
            txt = r.content[0].text
            print("  返回：", txt)
            if "/ 100" not in txt:
                print("❌ zombie_index 返回异常"); ok = False

            print("\n--- 调用 autopsy（Markdown 报告）---")
            r = await s.call_tool("autopsy", {"path": SAMPLE})
            txt = r.content[0].text
            print("  前 12 行：")
            for line in txt.splitlines()[:12]:
                print("   ", line)
            if "僵尸指数" not in txt:
                print("❌ autopsy 报告异常"); ok = False

            print("\n--- 调用 autopsy_json ---")
            r = await s.call_tool("autopsy_json", {"path": SAMPLE})
            txt = r.content[0].text
            import json
            try:
                d = json.loads(txt)
                print(f"  JSON 可用：doc_count={d.get('doc_count')} "
                      f"zombie={d.get('zombie_index')} skipped={d.get('skipped_metrics')}")
            except Exception as e:  # noqa: BLE001
                print(f"❌ JSON 解析失败：{e}"); ok = False

            print("\n--- 错误路径：不存在的目录 ---")
            r = await s.call_tool("zombie_index", {"path": "D:/nope/nope"})
            txt = r.content[0].text
            print("  返回：", txt)
            if "❌" not in txt:
                print("❌ 错误路径没有友好提示"); ok = False

    print("\n冒烟结果：" + ("✅ MCP 可用" if ok else "❌ 有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
