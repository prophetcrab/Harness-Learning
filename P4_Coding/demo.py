"""demo.py —— 亲手试一试 core（用眼睛验收；pytest 是用断言验收）。

    cd P4_Coding
    python demo.py

每长一步（PLAN 的 M1 拆步表），这里就多一节：demo 与 pytest 看的是**同一组
语义**——一个是给人看的，一个是给门禁看的。
"""

from __future__ import annotations

from core import Context, DuplicateSlotError, MissingServiceError


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def main() -> None:
    ctx = Context()

    section("步 1：服务槽位 —— 提供 / 查找")
    ctx.provide("greeting", "你好")
    print("require('greeting') →", ctx.require("greeting"))
    print("get('nothing')      →", ctx.get("nothing"), "（可选依赖的取法：缺失给默认值）")
    print("services            →", ctx.services)

    section("fail loud 之一：重名 —— 报错，且原值原样还在")
    original = ctx.require("greeting")
    try:
        ctx.provide("greeting", "喂")
    except DuplicateSlotError as exc:
        print("抛了 DuplicateSlotError：", exc)
    print("原值还在吗？", ctx.require("greeting") is original)

    section("fail loud 之二：缺失 —— require / retract 都报错")
    try:
        ctx.require("nothing")
    except MissingServiceError as exc:
        print("抛了 MissingServiceError：", exc)

    section("换实现：retract 旧的、provide 新的（没有 set）")
    old = ctx.retract("greeting")
    ctx.provide("greeting", f"{old}（新实现）")
    print("require('greeting') →", ctx.require("greeting"))
    print("services            →", ctx.services)

    section("自足：空装配单也能起（现在它是一个空容器）")
    empty = Context()
    print("Context() 起来了，services =", empty.services)

    # ------------------------------------------------------------------
    # 步 2：插件装载与依赖驱动
    # ------------------------------------------------------------------

    section("步 2：插件装载与依赖驱动（消费者先来、提供者后到）")
    world = Context()

    class Store:
        name = "store"

        def setup(self, ctx):
            ctx.provide("store", {"items": []})

    class Reporter:
        name = "reporter"
        inject = ("store",)

        def setup(self, ctx):
            ctx.require("store")

            def cleanup():
                # 退位这一刻提供者还没拆，所以这里还取得到（"深的先退"）
                print("    reporter 退位；此刻 store 还在吗？→", ctx.get("store") is not None)

            return cleanup

    def show() -> None:
        print("    ", [(info.name, info.status) for info in world.plugins])

    print("消费者先来（依赖未齐 → pending）：")
    world.load_plugin(Reporter())
    show()

    print("提供者来了（依赖齐 → 两个都自动激活）：")
    world.load_plugin(Store())
    show()

    print("提供者走了（消费者自动退位）：")
    world.unload_plugin("store")
    show()

    print("提供者回来（消费者自动重新激活）：")
    world.load_plugin(Store())
    show()

    # ------------------------------------------------------------------
    # 步 3：effect 回卷（注册即回卷）
    # ------------------------------------------------------------------

    section("步 3：effect 回卷 —— 注册即回卷，卸载一个不剩")
    room = Context()
    log: list[str] = []

    class App:
        name = "app"

        def setup(self, ctx):
            ctx.provide("app.name", "演示应用")     # 登记 1：服务（自动挂回卷）
            ctx.effect("listener", self._listen)      # 登记 2：子 effect

            def cleanup():
                log.append("清理 app 自己的资源")

            return cleanup                            # 登记 3：插件自己的撤销动作

        def _listen(self):
            log.append("子 effect 装载（比如开了一个连接）")
            return lambda: log.append("子 effect 回卷（连接关了）")

    room.load_plugin(App())
    print("装载后：services =", room.services, "effects =", room.effects)

    print("卸载插件 → 回卷顺序（子先父后）：")
    room.unload_plugin("app")
    for step in log:
        print("    ", step)
    print("卸载后：services =", room.services, "effects =", room.effects)

    section("步 3 之二：装载失败不留半个（连子 effect 一起撤）")
    broken = Context()

    class Broken:
        name = "broken"

        def setup(self, ctx):
            ctx.provide("half", 1)
            ctx.effect("half-sub", lambda: (lambda: print("    子 effect 被回卷掉")))
            raise RuntimeError("setup 里炸了")

    try:
        broken.load_plugin(Broken())
    except RuntimeError as exc:
        print("装载抛了：", exc)
    print("残留的 services =", broken.services, "（应当是空的）")
    print("残留的 effects  =", broken.effects, "（应当是空的）")

    # ------------------------------------------------------------------
    # 步 4：事件（广播 / 瀑布）
    # ------------------------------------------------------------------

    section("步 4：广播 —— 订阅按注册顺序收到通知；退订随效果回卷")
    stage = Context()
    heard: list[str] = []

    class Audience:
        name = "audience"

        def setup(self, ctx):
            ctx.on("speak", lambda text: heard.append(f"观众听到了：{text}"))

    stage.load_plugin(Audience())
    stage.emit("speak", text="第一次")
    stage.unload_plugin("audience")
    stage.emit("speak", text="第二次（没人听了）")
    print("heard =", heard)

    section("步 4 之二：瀑布 —— 中间件链 / 策略插座（guard 插件的形状）")
    pipe = Context()

    def real_execute(tool: str, args: dict) -> str:
        return f"执行 {tool}({args}) 的结果"

    def timeout_policy(execute, **context):
        """策略插件：把执行动作包一层（超时、重试、记账都这么写）。"""
        print("      [timeout-policy] 我包住了执行动作")

        def guarded(tool, args):
            print(f"      [timeout-policy] 先检查：{tool} 要不要限时")
            return execute(tool, args)

        return guarded

    def audit_policy(execute, **context):
        """第二个策略：它看到的是上一个策略包好的动作。"""
        print("      [audit-policy] 我也包了一层（看到的是上一层的产物）")

        def logged(tool, args):
            print(f"      [audit-policy] 记一笔：{tool}")
            return execute(tool, args)

        return logged

    pipe.on("tool/execute", timeout_policy)
    pipe.on("tool/execute", audit_policy)
    final = pipe.waterfall("tool/execute", real_execute, session="demo")
    print("最终执行：", final("calc", {"expr": "2+3"}))
    print("→ 这就是'策略挂在插座上'：循环只管发事件，策略按需装卸")

    # ------------------------------------------------------------------
    # 步 5：条目与装配单（loader 起步）
    # ------------------------------------------------------------------

    section("步 5：装配单 —— 产品形态就是一份数据")
    from plugins.base.loader import LOADER_SERVICE, AssemblyError, loader_plugin

    def make_store(label: str = "默认店"):
        class Store:
            name = "store"

            def __init__(self):
                self.label = label

            def setup(self, ctx):
                ctx.provide("store", self)

        return Store()

    def make_greeter(text: str = "你好"):
        class Greeter:
            name = "greeter"

            def setup(self, ctx):
                ctx.provide("greeting", text)

        return Greeter()

    def make_clock(tick: str = "滴答"):
        class Clock:
            name = "clock"

            def setup(self, ctx):
                ctx.provide("clock", tick)

        return Clock()

    shop = Context()
    shop.load_plugin(
        loader_plugin({"store": make_store, "greeter": make_greeter, "clock": make_clock})
    )
    loader = shop.require(LOADER_SERVICE)

    manifest = [
        {"id": "main", "name": "store", "config": {"label": "旗舰店"}},
        {"id": "ui", "name": "greeter", "config": {"text": "欢迎光临"}},
    ]
    print("装配单：", manifest)
    states = loader.assemble(manifest)
    for state in states:
        print(f"     {state.path:8} {state.name:8} {state.status}  插件={state.plugin}")
    print("config 生效：store.label =", shop.require("store").label)

    section("步 5 之二：装配失败 —— 整批回滚（不留半个世界）")
    print("再装一份单子：第一条好（clock）、第二条名字不在目录里")
    try:
        loader.assemble(
            [
                {"id": "extra", "name": "clock"},
                {"id": "ghost", "name": "不存在的东西"},
            ]
        )
    except AssemblyError as exc:
        print("装配抛了 AssemblyError：", exc)
    print("回滚检查：clock 装上又被撤掉了吗？→", shop.get("clock") is None)
    print("原有世界没事：store.label =", shop.require("store").label)

    # ------------------------------------------------------------------
    # 步 6：树与所有权级联
    # ------------------------------------------------------------------

    section("步 6：树 —— 开关以子树为单位，卸载卸父先卸子")
    from core import Context as Ctx

    def tree_factory(name, provides, inject=()):
        def factory(**_config):
            class P:
                pass

            P.name = name
            P.inject = tuple(inject)

            def setup(self, ctx):
                for dep in inject:
                    ctx.require(dep)
                for service in provides:
                    ctx.provide(service, name)

            P.setup = setup
            return P()

        return factory

    world = Ctx()
    world.load_plugin(
        loader_plugin(
            {
                "app": tree_factory("app", ("app-svc",)),
                "db": tree_factory("db", ("db-svc",), ("app-svc",)),
                "page": tree_factory("page", ("page-svc",), ("app-svc",)),
            }
        )
    )
    tree_loader = world.require(LOADER_SERVICE)
    tree_loader.assemble(
        [
            {
                "id": "app",
                "name": "app",
                "children": [
                    {"id": "db", "name": "db"},
                    {"id": "page", "name": "page"},
                ],
            }
        ]
    )

    def show_tree(title: str) -> None:
        print(f"     {title}")
        for state in tree_loader.snapshot():
            depth = state.path.count(":")
            print(f"       {'  ' * depth}{state.path.split(':')[-1]:6} {state.status}")

    show_tree("装配后的树：")
    print("services =", [s for s in world.services if s != "loader"])

    affected = tree_loader.disable("app")
    show_tree("停用 app 之后的树（整层，深的先退）：")
    print("     被停用的条目：", affected)
    print("     services =", [s for s in world.services if s != "loader"])

    tree_loader.enable("app")
    show_tree("启用 app 之后的树（浅的先来）：")
    print("     services =", [s for s in world.services if s != "loader"])

    tree_loader.unload("app")
    show_tree("卸载 app 之后的树（条目离开树）：")
    print("     树空了吗？", tree_loader.snapshot() == ())

    # ------------------------------------------------------------------
    # 步 7：热插拔
    # ------------------------------------------------------------------

    section("步 7：热插拔 —— 运行中加一条 / 换配置 / 换实现")
    hot = Ctx()
    hot.load_plugin(
        loader_plugin(
            {
                "greeter": tree_factory("greeter", ("greeting",)),
                "polite": tree_factory("polite", ("farewell",), ("greeting",)),
            }
        )
    )
    hot_loader = hot.require(LOADER_SERVICE)
    hot_loader.assemble([{"id": "hello", "name": "greeter"}])
    print("初始 services =", [s for s in hot.services if s != "loader"])

    rev = hot.revision
    hot_loader.load({"id": "bye", "name": "polite"})   # 运行中加一条
    print("load 一条后 services =", [s for s in hot.services if s != "loader"])
    print("revision：", rev, "→", hot.revision, "；变更报告：",
          f"退位={hot.last_change.retired} 激活={hot.last_change.activated}")

    # 换配置：用同一件插件、不同参数（演示里用一个 flag 让新实例提供不同的值）
    section("步 7 之二：update —— 造新失败时旧世界原样还在")
    from plugins.base.loader import AssemblyError as LoaderError

    def make_shop(label="旧店", boom=False):
        class Shop:
            name = "shop"

            def setup(self, ctx):
                if boom:
                    raise RuntimeError("新配置的 setup 炸了")
                ctx.provide("shop", label)

        return Shop()

    shop_ctx = Ctx()
    shop_ctx.load_plugin(loader_plugin({"shop": make_shop}))
    shop_loader = shop_ctx.require(LOADER_SERVICE)
    shop_loader.assemble([{"id": "shop", "name": "shop", "config": {"label": "旧店"}}])
    print("更新前：shop =", shop_ctx.require("shop"))

    try:
        shop_loader.update("shop", {"label": "新店", "boom": True})
    except LoaderError as exc:
        print("update 抛了 AssemblyError：", exc)
    print("失败之后：shop =", shop_ctx.require("shop"), "（旧世界原样还在）")

    shop_loader.update("shop", {"label": "新店"})
    print("成功更新：shop =", shop_ctx.require("shop"))

    # ------------------------------------------------------------------
    # 步 8：启动器（core · 引导）+ 产品政策
    # ------------------------------------------------------------------

    section("步 8：启动器 —— boot(装配单) 一条命令起树")
    from core import BootError, PolicyError, boot

    catalog = {
        "store": tree_factory("store", ("store",)),
        "ui": tree_factory("ui", ("ui",), ("store",)),
        "clock": tree_factory("clock", ("clock",)),
    }
    runtime = boot(
        [
            {"id": "store", "name": "store", "role": "base"},
            {"id": "ui", "name": "ui", "role": "application"},
        ],
        catalog=catalog,
        product=True,   # 这是"起产品"：要检查角色与应用化插件
    )
    try:
        print("boot 成功，--dump：")
        for line in runtime.dump().splitlines():
            print("    ", line)
        print("services =", [s for s in runtime.ctx.services if s != "loader"])
    finally:
        runtime.close()
    print("close 之后 plugins =", runtime.ctx.plugins)

    section("步 8 之二：产品政策 fail loud —— 没有应用化插件")
    try:
        boot(
            [{"id": "store", "name": "store", "role": "base"}],
            catalog=catalog,
            product=True,
        )
    except PolicyError as exc:
        print("抛了 PolicyError：", exc)

    section("步 8 之三：空装配单也能起（基座自足）；裸装配不查产品政策")
    bare = boot([], catalog=catalog)
    print("空装配单 →", bare.dump())
    bare.close()

    plain = boot([{"id": "store", "name": "store"}], catalog=catalog)  # 没 role
    print("裸装配（product=False）→ 起来了，条目数：", len(plain.snapshot()))
    plain.close()

    try:
        boot([{"id": "x", "name": "不存在"}], catalog=catalog)
    except BootError as exc:
        print("装配失败抛 BootError：", exc)

    # ------------------------------------------------------------------
    # 补充：动态依赖（ctx.inject）
    # ------------------------------------------------------------------

    section("补充：动态依赖 —— 插件主体先跑，依赖来了才装那一块")
    dyn = Ctx()
    log: list[str] = []

    def register_panel(c):
        print("     [回调] store 出现了：把面板注册进去")
        c.provide("panel", "面板")
        return lambda: print("     [回调] store 走了：面板自动拆掉")

    dyn.inject(["store"], register_panel)
    print("此时 injections =", [(i.deps, i.status) for i in dyn.injections])

    print("提供 store：")
    dyn.provide("store", "一号店")
    print("     panel =", dyn.get("panel"),
          "；injections =", [(i.deps, i.status) for i in dyn.injections])

    print("撤掉 store（依赖走了 → 那一块自动拆，订阅保留）：")
    dyn.retract("store")
    print("     panel =", dyn.get("panel"),
          "；injections =", [(i.deps, i.status) for i in dyn.injections])

    print("再提供 store（依赖回来 → 回调重跑）：")
    dyn.provide("store", "二号店")
    print("     panel =", dyn.get("panel"),
          "；revision =", dyn.revision,
          "；最近变更的注入报告 =", dyn.last_change.injections_started)

    # ------------------------------------------------------------------
    # M2 第一步：LLM 路由层（模型选择是数据）
    # ------------------------------------------------------------------

    section("M2：LLM 路由 —— 一个服务，多条路由；模型由请求决定")
    from plugins.base.llm import LLM_SERVICE, Message, assemble_text, llm_plugin
    from plugins.capability.llm.fake import fake_llm_plugin

    ai_ctx = Ctx()
    ai_ctx.load_plugin(llm_plugin(default_provider="fast", default_model="fast-1"))
    ai_ctx.load_plugin(fake_llm_plugin(route="fast", model="fast-1", replies=["快速模型的回答"]))
    ai_ctx.load_plugin(
        fake_llm_plugin(
            route="reason",
            model="reason-1",
            context_window=1_000_000,
            reasoning_efforts=("low", "high"),
            replies=["推理模型的回答"],
        )
    )
    llm = ai_ctx.require(LLM_SERVICE)

    print("已注册路由 =", llm.list_providers())
    for route in llm.list_providers():
        for info in llm.models(route):
            print(f"     {route}/{info.model}: 窗口 {info.context_window:,}"
                  f"，输出上限 {info.max_output_tokens}")

    def ask(provider: str, model: str, text: str, **extra) -> str:
        request = llm.request(
            [Message(role="user", content=text)],
            provider=provider, model=model, **extra,
        )
        return assemble_text(list(llm.stream(request)))

    print("会话 A（fast/fast-1）→", ask("fast", "fast-1", "你好"))
    print("会话 B（reason/reason-1，high）→",
          ask("reason", "reason-1", "仔细算一下", reasoning_effort="high"))
    print("子代理覆盖成 fast/fast-1 →", ask("fast", "fast-1", "子任务"))
    print("→ 模型选择只是请求里的两个字段：不同会话、不同子代理各选各的")

    # ------------------------------------------------------------------
    # M2：LLM 三个子插件（provider / retry / tokens-usages）
    # ------------------------------------------------------------------

    section("M2：子插件不齐也能跑，缺谁报告谁")
    from plugins.capability.llm.retry import llm_retry_plugin
    from plugins.capability.llm.tokens_usages import llm_tokens_usages_plugin

    sub_ctx = Ctx()
    sub_ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    sub_ctx.load_plugin(fake_llm_plugin(replies=["离线回复"], cached_input_tokens=40))
    sub_llm = sub_ctx.require(LLM_SERVICE)
    print("刚起时：status =", sub_llm.status())

    sub_ctx.load_plugin(llm_retry_plugin(max_attempts=3))
    print("装上 retry 后：missing =", sub_llm.missing_sub_plugins())

    sub_ctx.load_plugin(llm_tokens_usages_plugin())
    print("装上统计后：missing =", sub_llm.missing_sub_plugins(),
          "（只剩 llm-provider，因为用的是假适配器）")

    section("M2 之二：消耗统计 —— 总量 / 日（按用户）/ 本对话 + 缓存命中率")
    store = sub_ctx.require("llm-tokens-usages")

    def ask_session(session: str, user: str, text: str = "你好世界" * 40) -> None:
        request = sub_llm.request(
            [Message(role="user", content=text)],
            session_id=session, user_id=user,
        )
        list(sub_llm.stream(request))

    ask_session("s1", "alice")
    ask_session("s1", "alice")
    ask_session("s2", "bob")

    snap = store.snapshot()
    print("总量：", snap["totals"])
    print("今日合计：", snap["today_total"])
    for user, bucket in snap["today_by_user"].items():
        print(f"今日/{user}：", bucket)
    for session, bucket in snap["sessions"].items():
        print(f"会话 {session}：", bucket)

    # ------------------------------------------------------------------
    # M2：prompt（base）+ character-admin / system-prompt-admin（能力）
    # ------------------------------------------------------------------

    section("M2：提示词 —— 有序 section + 运行时变量")
    from plugins.base.prompt import PROMPT_SERVICE, prompt_plugin
    from plugins.capability.prompt.character_admin import character_admin_plugin
    from plugins.capability.prompt.system_prompt_admin import (
        SYSTEM_PROMPT_ADMIN_SERVICE,
        system_prompt_admin_plugin,
    )

    prompt_ctx = Ctx()
    prompt_ctx.load_plugin(
        prompt_plugin(
            sections=[
                {"name": "identity", "order": 10, "text": "你是一名严谨的工程师。"},
                {"name": "rules", "order": 20, "text": "先读代码再动手。"},
                {"name": "where", "order": 30, "text": "当前目录：{{cwd}}"},
            ]
        )
    )
    prompt = prompt_ctx.require(PROMPT_SERVICE)
    prompt.variable("cwd", lambda context: context.get("cwd", "（未知）"))
    print(prompt.render(cwd="/repo").text)
    print("指纹：", prompt.render(cwd="/repo").fingerprint)

    section("M2 之二：character-admin —— 不同会话用不同角色")
    prompt_ctx.load_plugin(character_admin_plugin(default="default"))
    characters = prompt_ctx.require("character-admin")
    characters.set("s1", "coder")
    characters.set("s2", "teacher")
    print("s1 的角色：", characters.active_name("s1"))
    print("s2 的角色：", characters.active_name("s2"))
    print("s1 渲染出的第一段：", prompt.render(session_id="s1", cwd="/repo").text.splitlines()[0])

    section("M2 之三：system-prompt-admin —— 检查与编辑")
    prompt_ctx.load_plugin(system_prompt_admin_plugin())
    admin = prompt_ctx.require(SYSTEM_PROMPT_ADMIN_SERVICE)

    admin.override("s1", "请用中文回答，并给出验证方式。")
    admin.disable("rules")
    view = admin.inspect(session_id="s1", cwd="/repo")
    print("编辑后的提示词：")
    for line in view["text"].splitlines():
        print("    ", line)
    print("section 表：", [(s["name"], s["enabled"]) for s in view["sections"]])
    print("指纹：", view["fingerprint"])

    prompt_ctx.unload_plugin("system-prompt-admin")
    print("卸载管理插件后，rules 恢复了吗？", prompt.is_enabled("rules"))

    # ------------------------------------------------------------------
    # M2：sessions / JSONL 持久化 / workspace / project
    # ------------------------------------------------------------------

    section("M2：会话 —— 事件是唯一真相，消息由投影派生")
    import tempfile
    from pathlib import Path

    from plugins.base.project import PROJECT_SERVICE, project_plugin
    from plugins.base.sessions import (
        ASSISTANT,
        SESSIONS_SERVICE,
        TOOL_CALL,
        TOOL_RESULT,
        TURN_END,
        TURN_START,
        sessions_plugin,
    )
    from plugins.base.workspace import WORKSPACE_SERVICE, workspace_plugin
    from plugins.capability.session.store_jsonl import (
        SESSION_STORE_SERVICE,
        session_store_jsonl_plugin,
    )
    from plugins.capability.workspace.admin import (
        WORKSPACE_ADMIN_SERVICE,
        workspace_admin_plugin,
    )

    data_dir = Path(tempfile.mkdtemp(prefix="p4-demo-"))
    ws_root = data_dir / "workspace"
    ws_root.mkdir()
    (ws_root / "hello.txt").write_text("hello", encoding="utf-8")

    project_ctx = Ctx()
    project_ctx.load_plugin(workspace_plugin(root=ws_root))
    project_ctx.load_plugin(workspace_admin_plugin())
    project_ctx.load_plugin(sessions_plugin())
    project_ctx.load_plugin(session_store_jsonl_plugin(root=data_dir / "sessions"))
    project_ctx.load_plugin(project_plugin())

    sessions = project_ctx.require(SESSIONS_SERVICE)
    sessions.open("s1", system_prompt="你是一名工程师。")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "看看工作区有什么"})
    sessions.append(
        "s1", ASSISTANT,
        {"content": "我先读一下目录", "tool_calls": [
            {"id": "c1", "name": "read_file", "arguments": '{"path":"hello.txt"}'}
        ]},
    )
    sessions.append("s1", TOOL_CALL, {"call_id": "c1", "name": "read_file"})
    sessions.append("s1", TOOL_RESULT, {"call_id": "c1", "status": "ok", "content": "hello"})
    sessions.append("s1", ASSISTANT, {"content": "工作区里只有一个 hello.txt。"})
    sessions.append("s1", TURN_END, {"turn": 1, "status": "done", "final_text": "只有一个 hello.txt。"})

    print("事件数：", len(sessions.events("s1")))
    print("投影出的消息：")
    for message in sessions.messages("s1"):
        extra = f"  tool_calls={[c.name for c in message.tool_calls]}" if message.tool_calls else ""
        print(f"    [{message.role}] {message.content!r}{extra}")

    section("M2 之二：JSONL 落盘 + 恢复（新进程读同一个目录）")
    store = project_ctx.require(SESSION_STORE_SERVICE)
    print("落盘文件：", store.path_for("s1").name,
          "（", store.path_for("s1").stat().st_size, "字节）")

    reloaded_ctx = Ctx()
    reloaded_ctx.load_plugin(sessions_plugin())
    reloaded_ctx.load_plugin(session_store_jsonl_plugin(root=data_dir / "sessions"))
    reloaded = reloaded_ctx.require(SESSIONS_SERVICE)
    session, resumed = reloaded.open("s1")
    print("恢复：resumed =", resumed, "，消息数 =", len(reloaded.messages("s1")))
    print("系统提示词：", session.effective_system_prompt())

    section("M2 之三：workspace —— 边界 / 管理面 / project")
    workspace = project_ctx.require(WORKSPACE_SERVICE)
    print("工作区：", workspace.info())
    try:
        workspace.resolve("../逃出去")
    except Exception as exc:
        print("越界被拒：", exc)

    admin = project_ctx.require(WORKSPACE_ADMIN_SERVICE)
    other = data_dir / "other"
    other.mkdir()
    (other / "x.txt").write_text("x" * 100, encoding="utf-8")
    admin.create("other", other)
    admin.set_active("other")
    print("工作区列表：", [(row["name"], row["human_size"], row["active"]) for row in admin.list()])

    project = project_ctx.require(PROJECT_SERVICE)
    print("项目状态：会话 =", project.info()["sessions"],
          "；存储目录 =", project.info()["store"]["root"])

    # ------------------------------------------------------------------
    # M2：工具面（tools-admin + fs 沙箱 + 六个工具插件）
    # ------------------------------------------------------------------

    section("M2：工具面 —— 注册 / 管线 / 六个工具")
    from plugins.base.fs import AutoApprove, fs_plugin
    from plugins.base.proc import proc_plugin
    from plugins.base.tools_admin import TOOL_ADMIN_SERVICE, tools_admin_plugin
    from plugins.capability.fs.observation_policy import fs_observation_policy_plugin
    from plugins.capability.fs.sandbox import fs_sandbox_plugin
    from plugins.capability.proc.backends import (
        proc_git_bash_plugin,
        proc_scripted_plugin,
    )
    from plugins.capability.tools.ask_user import tool_ask_user_plugin
    from plugins.capability.tools.fs import tool_fs_plugin
    from plugins.capability.tools.fs_search import tool_fs_search_plugin
    from plugins.capability.tools.math import tool_math_plugin
    from plugins.capability.tools.shell import tool_shell_plugin
    from plugins.capability.tools.web_search import tool_web_search_plugin

    tool_dir = Path(tempfile.mkdtemp(prefix="p4-tools-"))
    tool_ws = tool_dir / "ws"
    tool_ws.mkdir()
    (tool_ws / "hello.py").write_text("print('hi')\n", encoding="utf-8")

    tool_ctx = Ctx()
    tool_ctx.load_plugin(workspace_plugin(root=tool_ws))
    tool_ctx.load_plugin(fs_plugin())
    tool_ctx.load_plugin(proc_plugin())
    tool_ctx.load_plugin(proc_git_bash_plugin() if __import__("sys").platform == "win32" else proc_scripted_plugin())
    tool_ctx.load_plugin(tools_admin_plugin(approval=AutoApprove()))
    tool_ctx.load_plugin(fs_sandbox_plugin(mode="changes", approval=AutoApprove()))
    tool_ctx.load_plugin(fs_observation_policy_plugin())
    for plugin in (
        tool_math_plugin(),
        tool_fs_plugin(),
        tool_shell_plugin(),
        tool_fs_search_plugin(),
        tool_web_search_plugin(transport=lambda url, timeout: "示例页面正文"),
        tool_ask_user_plugin(asker=lambda q, opts, custom: opts[0]),
    ):
        tool_ctx.load_plugin(plugin)
    tools = tool_ctx.require(TOOL_ADMIN_SERVICE)

    print("工具面（按 owner）：")
    for owner, names in tools.owners().items():
        print(f"    {owner:14} {names}")
    print("模型可见（前 6 个）：", [spec["name"] for spec in tools.specs()][:6], "…")

    def run(name: str, args: dict) -> None:
        result = tools.execute(name, args)
        print(f"    {name:16} → {result.status:8} {str(result.content)[:80]}")
        if result.message:
            print(f"    {'':16}   message: {result.message[:100]}")

    run("calc", {"expression": "2 * (3 + 4)"})
    run("math", {"name": "factorial", "args": [5]})
    run("read_file", {"path": "hello.py"})
    run("write_file", {"path": "new.txt", "content": "新文件"})
    run("glob", {"pattern": "**/*.py"})
    if __import__("sys").platform == "win32":
        run("shell", {"command": "echo 你好 && ls"})
        run("shell", {"command": "dir /b"})   # 方言写错 → 拒绝并提示

    section("M2 之二：策略 —— 读后写 / 沙箱模式 / 工具装卸")
    (tool_ws / "unread.txt").write_text("还没人读过\n", encoding="utf-8")
    blocked = tools.execute("write_file", {"path": "unread.txt", "content": "盲改"})
    print("没读就改 →", blocked.status, "：", blocked.message[:80])
    tools.execute("read_file", {"path": "unread.txt"})
    allowed = tools.execute("write_file", {"path": "unread.txt", "content": "看过再改"})
    print("读过再改 →", allowed.status)

    tools.set_enabled("calc", False)
    print("停用 calc 后模型可见吗？", "calc" in [spec["name"] for spec in tools.specs()])
    print("直接调用 calc →", tools.execute("calc", {"expression": "1+1"}).status)
    tools.set_enabled("calc", True)
    tool_ctx.unload_plugin("tool-math")
    print("卸载 tool-math 后：calc =", tools.get("calc"))

    # ------------------------------------------------------------------
    # M2：agent / agent-loop / agent-admin —— 跑一个真 turn
    # ------------------------------------------------------------------

    section("M2：agent —— 一个真 turn（模型 → 工具 → 回灌 → 回答）")
    from plugins.base.agent import agent_plugin
    from plugins.base.llm import ScriptedReply, ToolCall
    from plugins.capability.agent.admin import AGENT_ADMIN_SERVICE, AgentMode, agent_admin_plugin
    from plugins.capability.agent.loop import agent_loop_plugin

    agent_ctx = Ctx()
    agent_ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    agent_ctx.load_plugin(
        fake_llm_plugin(
            replies=[
                ScriptedReply(
                    text="我先算一下",
                    tool_calls=(ToolCall(id="c1", name="calc", arguments='{"expression":"2+3"}'),),
                ),
                "结果是 5。",
            ]
        )
    )
    agent_ctx.load_plugin(prompt_plugin())
    agent_ctx.load_plugin(sessions_plugin())
    agent_ctx.load_plugin(tools_admin_plugin(approval=AutoApprove()))
    agent_ctx.load_plugin(tool_math_plugin())
    agent_ctx.load_plugin(agent_plugin())
    agent_ctx.load_plugin(agent_loop_plugin())
    agent_ctx.load_plugin(agent_admin_plugin())

    admin = agent_ctx.require(AGENT_ADMIN_SERVICE)
    print("可用模式：", admin.modes())
    handle = admin.create(mode="quick", session_id="demo-session", agent_id="main")
    result = handle.followup("帮我算 2+3")
    print("turn 结果：", {
        "status": result.status,
        "steps": result.steps,
        "final_text": result.final_text,
        "tokens": result.usage.total_tokens if result.usage else 0,
    })

    session_events = agent_ctx.require(SESSIONS_SERVICE).events("demo-session")
    print("落进日志的事件：", [event.kind for event in session_events])
    print("投影出的消息：")
    for message in agent_ctx.require(SESSIONS_SERVICE).messages("demo-session"):
        print(f"    [{message.role}] {message.content!r}")

    section("M2 之二：agent-admin —— 模式 / 停止 / 快照")
    admin.define(AgentMode(name="tiny", description="最多两步", max_steps=2))
    print("模式快照：", [mode["name"] for mode in admin.snapshot()["modes"]])
    print("在跑的 agent：", admin.snapshot()["agents"])
    print("停掉 main：", admin.stop("main"), "；剩余：", admin.list())


if __name__ == "__main__":
    main()













