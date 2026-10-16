"""步 2 验收：插件装载与依赖驱动。

对应 PLAN.md「M1 拆步」第 2 步：三个假插件的最小装配——装载顺序无关、
提供者来去 → 自动激活 / 退位、台账快照可读、失败不留半装。
"""

from __future__ import annotations

import pytest

from core import Context, DuplicatePluginError, MissingPluginError, MissingServiceError


def _info(ctx: Context, name: str):
    """取台账里某条记录（找不到就让测试炸——比静默 None 好）。"""
    return next(info for info in ctx.plugins if info.name == name)


# ---------------------------------------------------------------------------
# 三个假插件：提供者 / 消费者 / 变体（写测试不需要真 harness）
# ---------------------------------------------------------------------------


class Store:
    """提供者：提供 store 服务，无依赖。"""

    name = "store"

    def setup(self, ctx):
        ctx.provide("store", {"items": []})


class Reporter:
    """消费者：依赖 store。"""

    name = "reporter"
    inject = ("store",)

    def __init__(self):
        self.setups = 0

    def setup(self, ctx):
        self.setups += 1
        ctx.require("store")


class BetterStore:
    """另一份 store 实现（插件名不同、服务名相同）。"""

    name = "better-store"

    def setup(self, ctx):
        ctx.provide("store", {"items": ["x"]})


# ---------------------------------------------------------------------------
# 装载顺序无关 / 依赖驱动
# ---------------------------------------------------------------------------


def test_consumer_first_waits_then_activates():
    ctx = Context()
    reporter = Reporter()
    ctx.load_plugin(reporter)
    info = _info(ctx, "reporter")
    assert info.status == "pending"  # 待激活不是错误
    assert info.missing == ("store",)
    assert ctx.pending_plugins == ["reporter"]

    ctx.load_plugin(Store())
    assert _info(ctx, "reporter").status == "active"
    assert _info(ctx, "store").status == "active"
    assert reporter.setups == 1  # setup 只跑了一遍


def test_load_order_does_not_matter():
    first = Context()
    first.load_plugin(Reporter())
    first.load_plugin(Store())

    second = Context()
    second.load_plugin(Store())
    second.load_plugin(Reporter())

    assert [(i.name, i.status) for i in first.plugins] == [
        ("reporter", "active"),
        ("store", "active"),
    ]
    assert [(i.name, i.status) for i in second.plugins] == [
        ("store", "active"),
        ("reporter", "active"),
    ]
    assert first.services == second.services == ["store"]


def test_provide_and_retract_alone_drive_lifecycle():
    """不经过 load_plugin 的裸 provide / retract 同样驱动生命周期。"""
    ctx = Context()
    ctx.load_plugin(Reporter())
    ctx.provide("store", {"items": []})
    assert _info(ctx, "reporter").status == "active"
    ctx.retract("store")
    assert _info(ctx, "reporter").status == "pending"
    assert _info(ctx, "reporter").missing == ("store",)


def test_provider_leaving_deactivates_dependents():
    ctx = Context()
    ctx.load_plugin(Reporter())
    ctx.load_plugin(Store())
    ctx.unload_plugin("store")
    assert "store" not in ctx.services
    assert _info(ctx, "reporter").status == "pending"
    assert _info(ctx, "reporter").missing == ("store",)


def test_dependents_retire_before_provider():
    """"深的先退"：依赖者清理时，它依赖的服务还活着。"""

    class Watcher:
        name = "watcher"
        inject = ("store",)

        def __init__(self):
            self.saw_service_on_cleanup = None

        def setup(self, ctx):
            ctx.require("store")

            def cleanup():
                self.saw_service_on_cleanup = ctx.get("store") is not None

            return cleanup

    ctx = Context()
    watcher = Watcher()
    ctx.load_plugin(watcher)
    ctx.load_plugin(Store())
    ctx.unload_plugin("store")
    assert watcher.saw_service_on_cleanup is True  # 清理时服务仍在
    assert "store" not in ctx.services  # 之后才撤掉


def test_dependency_is_by_service_not_plugin_name():
    ctx = Context()
    ctx.load_plugin(Reporter())
    ctx.load_plugin(Store())
    ctx.unload_plugin("store")
    ctx.load_plugin(BetterStore())  # 换了实现、同一个服务名
    assert _info(ctx, "reporter").status == "active"


def test_loading_inside_setup_is_legal():
    """重入合法：插件在自己的 setup 里装载别的插件（外层 settle 收尾）。"""

    class Hub:
        name = "hub"

        def setup(self, ctx):
            ctx.load_plugin(Store())

    ctx = Context()
    ctx.load_plugin(Hub())
    assert _info(ctx, "hub").status == "active"
    assert _info(ctx, "store").status == "active"


# ---------------------------------------------------------------------------
# 台账快照
# ---------------------------------------------------------------------------


def test_snapshot_is_readable():
    ctx = Context()
    ctx.load_plugin(Reporter())
    ctx.load_plugin(Store())
    reporter = _info(ctx, "reporter")
    store = _info(ctx, "store")
    assert reporter.inject == ("store",)
    assert store.provided == ("store",)
    assert ctx.active_plugins == ["reporter", "store"]
    assert ctx.pending_plugins == []


# ---------------------------------------------------------------------------
# 停用 / 启用：回卷但保留登记
# ---------------------------------------------------------------------------


def test_disable_keeps_registration_and_reports_cascade():
    ctx = Context()
    ctx.load_plugin(Reporter())
    ctx.load_plugin(Store())

    affected = ctx.disable_plugin("store")
    assert affected == ["store", "reporter"]
    assert "store" not in ctx.services
    assert _info(ctx, "store").status == "disabled"
    assert _info(ctx, "reporter").status == "pending"
    assert ctx.disabled_plugins == ["store"]
    assert "store" in [i.name for i in ctx.plugins]  # 登记还在

    ctx.enable_plugin("store")
    assert _info(ctx, "store").status == "active"
    assert _info(ctx, "reporter").status == "active"


def test_disable_and_enable_guard_rails():
    ctx = Context()
    ctx.load_plugin(Store())
    ctx.disable_plugin("store")
    with pytest.raises(ValueError):
        ctx.disable_plugin("store")  # 已经停用
    with pytest.raises(MissingPluginError):
        ctx.enable_plugin("nothing")
    ctx.load_plugin(Reporter())  # pending（store 停用中）
    with pytest.raises(ValueError):
        ctx.enable_plugin("reporter")  # pending 不需要"启用"


# ---------------------------------------------------------------------------
# 可选依赖：缺了照常激活、来了不重装
# ---------------------------------------------------------------------------


def test_optional_missing_does_not_gate_or_restart():
    class Ticker:
        name = "ticker"
        optional = ("clock",)

        def __init__(self):
            self.setups = 0

        def setup(self, ctx):
            self.setups += 1
            self.clock = ctx.get("clock")

    class Clock:
        name = "clock"

        def setup(self, ctx):
            ctx.provide("clock", "tick")

    ctx = Context()
    ticker = Ticker()
    ctx.load_plugin(ticker)
    info = _info(ctx, "ticker")
    assert info.status == "active"  # 可选缺失不拦激活
    assert info.missing_optionals == ("clock",)

    ctx.load_plugin(Clock())
    assert ticker.setups == 1  # 依赖来了也不重装
    assert ticker.clock is None  # 用 ctx.get 容错：当时取不到


# ---------------------------------------------------------------------------
# fail loud 与不留半装
# ---------------------------------------------------------------------------


def test_duplicate_plugin_name_fails_loud():
    ctx = Context()
    ctx.load_plugin(Store())
    with pytest.raises(DuplicatePluginError):
        ctx.load_plugin(Store())


def test_invalid_declarations_fail_loud():
    ctx = Context()
    with pytest.raises(TypeError):
        ctx.load_plugin(object())  # 没有 setup

    class BadInject:
        name = "bad-inject"
        inject = "store"  # 字符串不是序列（会被逐字符迭代）

        def setup(self, ctx):
            pass

    with pytest.raises(TypeError):
        ctx.load_plugin(BadInject())

    class Overlap:
        name = "overlap"
        inject = ("store",)
        optional = ("store",)

        def setup(self, ctx):
            pass

    with pytest.raises(ValueError):
        ctx.load_plugin(Overlap())

    assert ctx.plugins == []  # 声明非法：连台账都没进


def test_failed_load_leaves_no_half_state():
    class Bad:
        name = "bad"

        def setup(self, ctx):
            ctx.provide("half", 1)  # 先装一半……
            raise RuntimeError("boom")

    ctx = Context()
    ctx.provide("keep", "原始值")
    with pytest.raises(RuntimeError):
        ctx.load_plugin(Bad())
    assert ctx.services == ["keep"]  # 半装被整表还原
    assert ctx.require("keep") == "原始值"
    assert ctx.plugins == []  # 失败记录不留在台账


def test_later_activation_failure_marks_failed_and_can_retry():
    class Flaky:
        name = "flaky"
        inject = ("store",)

        def __init__(self):
            self.attempts = 0

        def setup(self, ctx):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("第一次必炸")
            ctx.provide("flaky", "ok")

    ctx = Context()
    ctx.load_plugin(Flaky())  # pending：依赖未齐，不会激活
    with pytest.raises(RuntimeError, match="第一次必炸"):
        ctx.load_plugin(Store())  # 依赖齐 → 激活 flaky → 炸（异常抛回触发方）

    info = _info(ctx, "flaky")
    assert info.status == "failed"
    assert isinstance(info.error, RuntimeError)
    assert _info(ctx, "store").status == "active"  # 提供者本身没事

    ctx.enable_plugin("flaky")  # 重试（enable 只允许 disabled / failed）
    assert _info(ctx, "flaky").status == "active"
    assert "flaky" in ctx.services


def test_unload_pending_is_fine_and_missing_fails_loud():
    ctx = Context()
    ctx.load_plugin(Reporter())  # pending：还没登记任何东西
    ctx.unload_plugin("reporter")
    assert ctx.plugins == []
    with pytest.raises(MissingPluginError):
        ctx.unload_plugin("nothing")
    with pytest.raises(MissingServiceError):
        ctx.retract("store")


def test_plugin_handle_is_idempotent_and_instance_bound():
    """装载返回的句柄认"这一次装载"：重复调用安全，同名新插件不被误伤。"""
    ctx = Context()
    handle = ctx.load_plugin(Store())
    handle()
    handle()  # 幂等：再调一次是空操作（不是 MissingPluginError）

    ctx.load_plugin(Store())  # 同名重新装载（新实例、新记录）
    handle()  # 旧句柄认的那一次已经不在了 → 空操作
    assert ctx.active_plugins == ["store"]

    ctx.unload_plugin("store")  # 按名字的 API 仍然严格
    assert ctx.active_plugins == []


def test_cleanup_failure_does_not_stop_the_unload_cascade():
    """级联不半途而废：一个依赖者的清理炸了，其余退位与卸载照常完成。"""
    log: list[str] = []

    def dependent(name: str, *, boom: bool = False) -> object:
        class D:
            pass

        D.name = name
        D.inject = ("p-svc",)

        def setup(self, ctx):
            ctx.require("p-svc")

            def cleanup():
                log.append(f"{name}:out")
                if boom:
                    raise RuntimeError(f"{name} 清理炸了")

            return cleanup

        D.setup = setup
        return D()

    class Provider:
        name = "provider"

        def setup(self, ctx):
            ctx.provide("p-svc", 1)
            return lambda: log.append("provider:out")

    ctx = Context()
    ctx.load_plugin(Provider())
    ctx.load_plugin(dependent("d1", boom=True))
    ctx.load_plugin(dependent("d2"))

    with pytest.raises(RuntimeError, match="d1 清理炸了"):
        ctx.unload_plugin("provider")

    assert log == ["d2:out", "d1:out", "provider:out"]  # 三件全跑了
    assert "p-svc" not in ctx.services  # 提供者的服务照常撤销
    # 提供者卸载、依赖者退回收 pending（记录留着：再来一个提供者它们就复活）
    assert [i.name for i in ctx.plugins] == ["d1", "d2"]
    assert all(i.status == "pending" for i in ctx.plugins)


def test_cleanup_failure_does_not_stop_the_disable_cascade():
    """停用的级联同理：一个清理炸了，其余照常退位（错误最后抛出）。"""
    log: list[str] = []

    class Provider:
        name = "provider"

        def setup(self, ctx):
            ctx.provide("p-svc", 1)

            def cleanup():
                log.append("provider:out")
                raise RuntimeError("provider 清理炸了")

            return cleanup

    class Dependent:
        name = "dependent"
        inject = ("p-svc",)

        def setup(self, ctx):
            ctx.require("p-svc")
            return lambda: log.append("dependent:out")

    ctx = Context()
    ctx.load_plugin(Provider())
    ctx.load_plugin(Dependent())

    with pytest.raises(RuntimeError, match="provider 清理炸了"):
        ctx.disable_plugin("provider")

    assert log == ["dependent:out", "provider:out"]  # 依赖者照常先退位
    assert "p-svc" not in ctx.services
    assert [i.name for i in ctx.plugins if i.status == "disabled"] == ["provider"]
