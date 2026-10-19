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
    from plugins.loader import LOADER_SERVICE, AssemblyError, loader_plugin

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
    from plugins.loader import AssemblyError as LoaderError

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


if __name__ == "__main__":
    main()






