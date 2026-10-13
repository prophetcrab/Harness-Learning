"""demo.py —— 全离线演示：插件化 harness 的一生（装载 → 对话 → 回卷）。

八节，全部离线（离线模型 + MemoryFS + 剧本命令，零副作用、不需要 key）：

    0) 装载：插件列表 → ctx（顺序打乱也能装配 —— inject 驱动）
    1) 台账：插件 / 服务 / 工具面（谁提供什么，一眼可读）
    2) 对话：一个 turn 的完整事件流（含工具调用）
    3) 日志：append-only 事件 + 落盘 + resume（重开进程续聊）
    4) 热插拔：运行时换 fs 插件（依赖者自动停用/重新激活）
    5) 卸载：整体回卷（服务归零；这就是"注册即 effect"）
    6) 界面也是插件：装载即起服务（端口可用）→ 卸载即停服（端口释放）
    7) RPC 也是插件：握手 → prompt → follow（重放 + 订阅，事件流实时生长）

运行：python demo.py            （--keep 保留 demo_run 目录供翻看）
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from components.llm import text_reply, tool_call_reply
from components.rpc import LineTransport
from plugins import (
    RPC_SERVICE,
    WEBUI_SERVICE,
    AgentPlugin,
    Booted,
    PromptPlugin,
    SessionsPlugin,
    ToolsPlugin,
    boot_offline,
    fake_llm_plugin,
    local_fs_plugin,
    memory_fs_plugin,
    rpc_plugin,
)

HERE = Path(__file__).resolve().parent
RUN_DIR = HERE / "demo_run"


def banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show_ledger(booted: Booted) -> None:
    print("  插件台账（顺序 = 装载顺序）：")
    for info in booted.ctx.plugins:
        inject = f"  inject={list(info.inject)}" if info.inject else ""
        print(f"    - {info.name:<28} [{info.status}]{inject}")
    print(f"  服务：{booted.ctx.services}")
    tools = booted.ctx.require("tools")
    print(f"  工具面（{len(tools.names)}）：{'、'.join(tools.names)}")


def section_0(root: Path, workspace: Path) -> None:
    banner("0) 装载：插件列表 → ctx（故意打乱顺序）")
    from kernel import Context

    # 顺序打乱：tools 在 fs 之前、agent 在 llm 之前——inject 会让它们等
    ctx = Context()
    ctx.load_plugins(
        [
            ToolsPlugin(workspace=workspace),   # 依赖 fs：此刻 fs 还没提供
            AgentPlugin(),                       # 依赖 llm/tools/sessions/prompt
            fake_llm_plugin([text_reply("hello")]),
            memory_fs_plugin(),
            SessionsPlugin(root),
            PromptPlugin(workspace=workspace),
        ]
    )
    print(f"  装载后活跃插件：{ctx.active_plugins}")
    print(f"  待激活：{ctx.pending_plugins}（空 = 全部就绪）")
    print("  → 装载顺序无关：依赖齐备的那一刻，插件自动激活。")
    for name in reversed(ctx.active_plugins):
        ctx.unload_plugin(name)


def section_1_2(root: Path, workspace: Path) -> None:
    banner("1) 台账 + 2) 对话：一个 turn 的完整事件流")
    booted = boot_offline(
        root=root,
        workspace=workspace,
        script=[
            tool_call_reply("calculate", {"expression": "2+3"}),
            text_reply("2+3=5，已用计算器核对。"),
        ],
    )
    show_ledger(booted)

    print("\n  你：帮我算 2+3")
    result = booted.agent.ask("帮我算 2+3", session_id="demo")
    print(f"  助手：{result.final_text}   （{len(result.steps)} 步，status={result.status}）")
    events = booted.agent.open("demo").session.events
    print(f"\n  事件流（{len(events)} 条，落盘 {root / 'demo' / 'session.jsonl'}）：")
    for event in events:
        print(f"    #{event.seq:<3} {event.type}")
    booted.close()
    print("\n  （已整体卸载——下一节重新装配，验证 resume）")


def section_3(root: Path, workspace: Path) -> None:
    banner("3) 日志：resume（重开进程、同名会话续聊）")
    booted = boot_offline(root=root, workspace=workspace, script=[text_reply("不客气，随时问。")])
    opened = booted.agent.open("demo")
    print(f"  重开会话 demo：resumed={opened.resumed}  repaired={opened.repaired}")
    print(f"  历史（由日志投影）：{[m.role for m in opened.messages]}")
    result = booted.agent.ask("谢谢", session_id="demo")
    print(f"  turn 接续：第 {result.turn} 个 turn（不是从 1 重来）")
    booted.close()


def section_4(root: Path, workspace: Path) -> None:
    banner("4) 热插拔：运行时换 fs 插件（依赖者自动停用/重激活）")
    booted = boot_offline(root=root, workspace=workspace, script=[text_reply("好的")])
    fs_name = next(name for name in booted.ctx.active_plugins if name.startswith("fs:"))
    print(f"  初始：fs 槽由 {fs_name} 提供")

    booted.ctx.unload_plugin(fs_name)
    print(f"  卸载 fs:MemoryFS → 活跃：{booted.ctx.active_plugins}")
    print(f"    待激活：{booted.ctx.pending_plugins}（tools/agent 依赖 fs，自动退位）")

    disk_ws = workspace.parent / "disk_ws"
    booted.ctx.load_plugin(local_fs_plugin(disk_ws))
    print(f"  换入 fs:LocalFS → 活跃：{booted.ctx.active_plugins}")
    print(f"    服务：{booted.ctx.services}")

    result = booted.agent.ask("现在用什么文件系统？", session_id="demo2")
    print(f"  新会话用新组合对话成功：{result.final_text}")
    booted.close()


def section_5(root: Path, workspace: Path) -> None:
    banner("5) 卸载：整体回卷（注册即 effect）")
    booted = boot_offline(root=root, workspace=workspace, script=[text_reply("ok")])
    booted.agent.ask("随便问一句", session_id="demo3")
    print(f"  卸载前：活跃插件 {booted.ctx.active_plugins}")
    print(f"          服务 {booted.ctx.services}")
    print(f"          事件订阅 {booted.ctx.event_names}")
    booted.close()
    print(f"  卸载后：活跃插件 {booted.ctx.active_plugins}")
    print(f"          服务 {booted.ctx.services}")
    print(f"          事件订阅 {booted.ctx.event_names}")
    print("  → 每样登记物都被各自的 effect 回卷；没有任何残留。")


def section_6(root: Path, workspace: Path) -> None:
    banner("6) 界面也是插件：装载即起服务 → 卸载即停服（端口释放）")
    import socket
    import urllib.request

    from plugins import webui_plugin

    booted = boot_offline(root=root, workspace=workspace, script=[text_reply("你好，我是界面里的回答。")])

    # 装载：端口交给系统分配（port=0），真实地址从 `webui` 服务里取。
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url
    print(f"  装载 webui 插件 → {url}")
    print(f"  活跃插件：{booted.ctx.active_plugins}")

    with urllib.request.urlopen(url + "api/ledger", timeout=5) as response:
        import json

        ledger = json.load(response)
    print(f"  GET /api/ledger：{len(ledger['plugins'])} 个插件、"
          f"{len(ledger['services'])} 个服务、{len(ledger['tools'])} 件工具")
    print(f"  页面上看得到：{'、'.join(p['name'] for p in ledger['plugins'])}")

    port = booted.ctx.require(WEBUI_SERVICE).port

    def port_open() -> bool:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    print(f"  卸载前端口 {port} 可用：{port_open()}")
    booted.ctx.unload_plugin("webui")
    print(f"  卸载 webui 插件 → 活跃：{booted.ctx.active_plugins}")
    print(f"  卸载后端口 {port} 可用：{port_open()}（服务停止、端口释放，无残留）")
    print(f"  服务：{booted.ctx.services}")
    booted.close()


def section_7(root: Path, workspace: Path) -> None:
    banner("7) RPC 也是插件：握手 → prompt → follow（重放 + 订阅）")
    import io

    booted = boot_offline(root=root, workspace=workspace, script=[text_reply("RPC 里的回答。")])
    booted.ctx.load_plugin(rpc_plugin())  # stdio 模式：不起端口，传输由入口驱动
    service = booted.ctx.require(RPC_SERVICE)

    out = io.StringIO()
    transport = LineTransport(io.StringIO(""), out, service)

    def call(method: str, params: dict, id: int) -> None:
        transport.handle_line(json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))

    def last() -> dict:
        return json.loads(out.getvalue().strip().splitlines()[-1])

    call("session.prompt", {"session": "rpc-demo", "text": "握手前调用"}, 1)
    print(f"  未握手就 prompt → 错误码 {last()['error']['code']}（-32002 NOT_INITIALIZED）")

    call("initialize", {}, 2)
    caps = last()["result"]
    print(f"  握手 → {caps['server']['name']} v{caps['server']['version']}，"
          f"方法 {caps['capabilities']['methods']}，工具面 {len(caps['capabilities']['tools'])} 件")

    call("session.follow", {"session": "rpc-demo", "from_seq": 0}, 3)
    print(f"  follow（空会话）→ {last()['result']}")

    def event_count() -> int:
        return len(
            [line for line in out.getvalue().strip().splitlines() if "session.event" in line]
        )

    call("session.prompt", {"session": "rpc-demo", "text": "第一问"}, 4)
    first = last()["result"]
    total = event_count()
    print(f"  prompt → turn={first['turn']} status={first['status']}；"
          f"订阅者实时收到 {total} 条 session.event（长 turn 中边跑边推）")

    max_seq = max(
        json.loads(line)["params"]["seq"]
        for line in out.getvalue().strip().splitlines()
        if "session.event" in line
    )
    fresh = io.StringIO()
    t2 = LineTransport(io.StringIO(""), fresh, service)
    t2.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    t2.handle_line(json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "session.follow",
         "params": {"session": "rpc-demo", "from_seq": max_seq}}
    ))
    print(f"  重连（from_seq={max_seq}）→ {json.loads(fresh.getvalue().strip().splitlines()[-1])['result']}"
          "（不重不漏：补缺靠重放日志，不靠缓存）")

    transport.serve_forever()  # reader 为空 → EOF → 注销该连接的订阅
    print(f"  连接 1 断开后订阅数：{service.subscription_count}（连接 2 仍在）")
    booted.ctx.unload_plugin("rpc")
    print(f"  卸载 rpc 插件 → 服务：{booted.ctx.services}")
    booted.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo.py", description="P3 插件化 harness 全离线演示")
    parser.add_argument("--keep", action="store_true", help="保留 demo_run 目录")
    args = parser.parse_args(argv)

    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    root = RUN_DIR / "sessions"
    workspace = RUN_DIR / "ws"
    workspace.mkdir(parents=True)

    print("P3 基础包演示：完全插件化的 mini harness（全离线）")
    section_0(root, workspace)
    section_1_2(root, workspace)
    section_3(root, workspace)
    section_4(root, workspace)
    section_5(root, workspace)
    section_6(root, workspace)
    section_7(root, workspace)

    banner("完成")
    print(f"  {'保留' if args.keep else '清理'} demo_run：{RUN_DIR}")
    if not args.keep:
        shutil.rmtree(RUN_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
