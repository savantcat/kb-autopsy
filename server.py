#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb-autopsy MCP 服务端

把「知识库尸检」暴露成 MCP 工具，让任何支持 MCP 的 Agent（Claude / Cursor / Hermes…）
可以直接调用：给它一个知识库目录，拿回僵尸指数与五项病理指标。

启动（stdio）：
    python server.py

在 Hermes 里注册：
    hermes mcp add kb-autopsy --command "C:/path/to/python.exe" --args "D:/path/kb-autopsy/server.py"
"""
from __future__ import annotations

import json
from pathlib import Path

# MCP SDK v2（≥2.0）把 FastMCP 改名为 MCPServer；v1 里叫 FastMCP。
# 两个版本都得兼容，否则换台机器就起不来（实测踩过：本机 mcp 2.1.1 直接 ImportError）。
try:                                          # v2
    from mcp.server import MCPServer as _Server
except ImportError:                           # v1
    try:
        from mcp.server.fastmcp import FastMCP as _Server
    except ImportError:
        raise SystemExit("需要 MCP SDK：pip install mcp")

import kb_autopsy as K

from mcp.types import ToolAnnotations

# 纯只读服务。四个 hint 必须**全部**声明：缺任一个会被 OpenAI 目录直接拒收，
# 第三方信任目录也会把它记成 quality finding。（这条在 mcp-server-publishing skill 里）
RO_ANN = ToolAnnotations(
    title="知识库尸检（只读）",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

mcp = _Server("kb-autopsy")


def _run(path: str, **overrides):
    p = Path(path).expanduser()
    if not p.is_dir():
        return None, f"❌ 不是目录：{path}"
    cfg = dict(K.DEFAULTS)
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return K.analyze(p, cfg), None


@mcp.tool(annotations=RO_ANN)
def autopsy(path: str, stale_days: int = 180) -> str:
    """给一个知识库目录做体检，返回 Markdown 报告（僵尸指数 + 五项病理指标）。

    核心命题：知识库不是买来的，是养出来的。没人负责、没有更新节奏、没有人在看，
    再好的系统也会变成僵尸。本工具把「有没有在养」变成可核验的数字。

    Args:
        path: 知识库目录的绝对路径（扫描其中的 .md/.markdown/.txt/.rst）
        stale_days: 多久没更新算「陈旧」，默认 180 天
    """
    r, err = _run(path, stale_days=stale_days)
    if err:
        return err
    return K.render_markdown(r)


@mcp.tool(annotations=RO_ANN)
def autopsy_json(path: str, stale_days: int = 180) -> str:
    """同 autopsy，但返回结构化 JSON（便于程序化处理或做趋势对比）。

    Args:
        path: 知识库目录的绝对路径
        stale_days: 多久没更新算「陈旧」，默认 180 天
    """
    r, err = _run(path, stale_days=stale_days)
    if err:
        return json.dumps({"error": err}, ensure_ascii=False)
    return json.dumps(r, ensure_ascii=False, indent=2)


@mcp.tool(annotations=RO_ANN)
def zombie_index(path: str, stale_days: int = 180) -> str:
    """只回一个数字：僵尸指数（0-100）。越高越接近「已经没人养了」。

    给「我要一眼看结论」的场景用。语料小于 5 篇时会明确拒绝出分，不会硬凑一个数。

    Args:
        path: 知识库目录的绝对路径
        stale_days: 多久没更新算「陈旧」，默认 180 天
    """
    r, err = _run(path, stale_days=stale_days)
    if err:
        return err
    if r.get("error"):
        return f"❌ {r['error']}"
    return f"{r['zombie_index']} / 100 —— {r['verdict']['emoji']}{r['verdict']['level']}"


@mcp.tool(annotations=RO_ANN)
def qa_autopsy(path: str) -> str:
    """给 AI 客服**问答库**（结构化 QA JSON）做体检，返回 Markdown 报告。

    用文档库的尺子量问答库是错的：文档库怕陈旧，问答库怕**同一个问题有两个不同答案**。
    五项病理：同题异答 / 无出处 / 空壳答案 / 答案复用 / 缺字段。

    支持形态：{"cluster": {...}, "atoms": [...]} 与 {"qa"|"questions"|"items": [...]} 及裸列表。

    Args:
        path: 问答库目录或单个 JSON 文件路径
    """
    import qa_autopsy as Q
    p = Path(path).expanduser()
    if not p.exists():
        return f"❌ 路径不存在：{path}"
    return Q.render_qa_markdown(Q.analyze_qa(p))


if __name__ == "__main__":
    mcp.run()
