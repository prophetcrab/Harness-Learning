"""app.py —— 最小应用入口：launcher + 装配单 → 一个 AI 服务。

    python app.py                          # 交互式对话（离线假模型）
    python app.py --realtime               # 交互式对话（真实 DeepSeek）
    python app.py --ask "帮我算 2+3"        # 跑一个 turn 后退出
    python app.py --realtime --ask "你好"   # 用真实模型跑一个 turn
    python app.py --manifest manifests/minimal.yaml   # 装配单换成 YAML
    python app.py --session s1 --ask "…"    # 指定会话（不存在就新建）
    python app.py --dump                    # 只装配并打印插件树，不起对话
    python app.py --sandbox free            # 沙箱模式（all / changes / risky / free）

**外部只需要两样东西**：本启动器 + 装配单（Python 的
`manifests/minimal.py` / `manifests/realtime.py`，或 YAML 的
`manifests/minimal.yaml`）。内部装配、插件树、会话、提示词都不需要外部知道。

**YAML 装配单**：`--manifest` 读一份 YAML（`manifests/load.py`），插件工厂
仍来自同一份 `catalog()` 白名单——YAML 是**数据**，不能凭空造代码。工作区 /
会话目录 / 沙箱模式由命令行参数填进占位符，改产品形态不用改 Python。

**真实模型**：`--realtime` 把模型后端换成 DeepSeek（密钥从环境变量取，
`app.py` 启动时加载项目根的 `.env`）。YAML 方式下两条路由都已装好，用
`--provider deepseek --model <模型名>` 切换，密钥同样只从环境变量取。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from core import BootError, PolicyError, Runtime, boot
from manifests.load import ManifestFileError
from manifests.minimal import catalog, manifest
from plugins.base.project import PROJECT_SERVICE
from plugins.capability.agent.admin import AGENT_ADMIN_SERVICE

HERE = Path(__file__).resolve().parent
#: 项目根（P4_Coding 的上一级）——`.env` 在那里。
PROJECT_ROOT = HERE.parent


def load_env(path: Path = PROJECT_ROOT / ".env") -> None:
    """把项目根的 `.env` 读进环境变量（已存在的环境变量优先）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def build_runtime(
    *,
    workspace: str,
    sessions: str,
    sandbox: str,
    realtime: bool = False,
    approval_policy: str = "ask",
    manifest_path: str | Path | None = None,
) -> Runtime:
    """按装配单起一个服务。

    三条路径（差别**只在装配数据**，启动器不认识具体模型）：

        manifest_path  读 YAML 装配单（`manifests/load.py`），目录仍来自代码
        realtime       `manifests/realtime.py`：真实端点 + 密钥环境变量
        默认           `manifests/minimal.py`：离线剧本

    **现在是 `product=False`**：本文件自己就是入口（终端 REPL），但"入口是
    插件"这条要到 M4 的 `cli` 插件才成立。装配单里还没有 `role=application`
    的条目，所以不能开产品政策检查——开了会正确地拒绝（"用户从哪进来"没有
    答案）。M4 加上 `cli` 插件后，这里改回 `product=True`。
    """
    if manifest_path is not None:
        from manifests.load import load_manifest

        entries = load_manifest(
            manifest_path,
            variables={
                "workspace": str(workspace),
                "sessions": str(sessions),
                "sandbox": sandbox,
                "approval_policy": approval_policy,
            },
        )
        plugin_catalog = catalog()
        return boot(entries, catalog=plugin_catalog, product=False)

    if realtime:
        from manifests.realtime import catalog as realtime_catalog
        from manifests.realtime import manifest as realtime_manifest

        entries = realtime_manifest(
            workspace=workspace,
            sessions=sessions,
            sandbox_mode=sandbox,
            approval_policy=approval_policy,
        )
        plugin_catalog = realtime_catalog()
    else:
        entries = manifest(
            workspace=workspace,
            sessions=sessions,
            sandbox_mode=sandbox,
            approval_policy=approval_policy,
        )
        plugin_catalog = catalog()
    return boot(entries, catalog=plugin_catalog, product=False)


def _start_handle(runtime: Runtime, *, session: str, mode: str | None,
                  provider: str | None, model: str | None):
    project = runtime.ctx.require(PROJECT_SERVICE)
    overrides = {}
    if provider:
        overrides["provider"] = provider
    if model:
        overrides["model"] = model
    return project.start(session_id=session, mode=mode, **overrides)


def _report_error(exc: BaseException) -> None:
    """把 agent 的错误说成人话（不打 traceback —— 那是给开发者看的）。"""
    print(f"错误：{exc}", file=sys.stderr)


def run_one(
    runtime: Runtime,
    *,
    session: str,
    text: str,
    mode: str | None,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    try:
        handle = _start_handle(runtime, session=session, mode=mode,
                               provider=provider, model=model)
    except Exception as exc:  # noqa: BLE001 —— 启动失败给一句人话
        _report_error(exc)
        return 2
    try:
        result = handle.followup(text)
    except Exception as exc:  # noqa: BLE001
        _report_error(exc)
        return 1
    finally:
        handle.dispose()
    if result is None:
        print("（turn 还在跑：这是后台执行的现象，单次模式下不该出现）", file=sys.stderr)
        return 1
    print(f"[{result.status}] {result.final_text}")
    if result.steps:
        print(f"  steps={result.steps} tokens={result.usage.total_tokens if result.usage else 0}",
              file=sys.stderr)
    return 0 if result.status == "done" else 1


def repl(
    runtime: Runtime,
    *,
    session: str,
    mode: str | None,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    try:
        handle = _start_handle(runtime, session=session, mode=mode,
                               provider=provider, model=model)
    except Exception as exc:  # noqa: BLE001
        _report_error(exc)
        return 2
    print(f"会话 {session}（模式 {handle.options.mode or 'default'}）。/exit 退出。", file=sys.stderr)
    try:
        while True:
            try:
                text = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                break
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            try:
                result = handle.followup(text)
            except Exception as exc:  # noqa: BLE001 —— 一次失败不该踢掉整个 REPL
                _report_error(exc)
                continue
            if result is not None:
                print(f"助手> {result.final_text}")
    finally:
        handle.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P4 最小 AI 服务")
    parser.add_argument("--ask", help="跑一个 turn 后退出")
    parser.add_argument("--session", default="default", help="会话 id（默认 default）")
    parser.add_argument("--mode", default=None, help="agent 模式（default/quick/careful）")
    parser.add_argument("--provider", default=None,
                        help="显式指定模型路由（覆盖会话记录；如 deepseek / fake）")
    parser.add_argument("--model", default=None, help="显式指定模型名")
    parser.add_argument("--workspace", default=str(HERE / "workspace"), help="工作区目录")
    parser.add_argument("--sessions", default=str(HERE / "sessions"), help="会话日志目录")
    parser.add_argument("--sandbox", default="changes",
                        choices=("all", "changes", "risky", "free"))
    parser.add_argument("--dump", action="store_true", help="只装配并打印插件树")
    parser.add_argument("--manifest", default=None,
                        help="用 YAML 装配单（如 manifests/minimal.yaml）；"
                             "与 --realtime 互斥")
    parser.add_argument("--realtime", action="store_true",
                        help="用真实模型（DeepSeek；密钥从 .env / 环境变量取）")
    parser.add_argument("--yes", action="store_true",
                        help="自动批准所有工具审批（等价于无人值守；默认交互式 y/n）")
    args = parser.parse_args(argv)
    if args.manifest and args.realtime:
        parser.error("--manifest 与 --realtime 互斥：YAML 里已经决定了模型路由，"
                     "用 --provider / --model 选择即可")

    load_env()
    Path(args.workspace).mkdir(parents=True, exist_ok=True)
    try:
        runtime = build_runtime(
            workspace=args.workspace,
            sessions=args.sessions,
            sandbox=args.sandbox,
            realtime=args.realtime,
            approval_policy="allow" if args.yes else "ask",
            manifest_path=args.manifest,
        )
    except (BootError, PolicyError, ManifestFileError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    try:
        if args.dump:
            print(runtime.dump())
            print()
            admin = runtime.ctx.require(AGENT_ADMIN_SERVICE)
            print("agent 模式：", ", ".join(admin.modes()))
            return 0
        if args.ask:
            return run_one(
                runtime,
                session=args.session,
                text=args.ask,
                mode=args.mode,
                provider=args.provider,
                model=args.model,
            )
        return repl(
            runtime,
            session=args.session,
            mode=args.mode,
            provider=args.provider,
            model=args.model,
        )
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
