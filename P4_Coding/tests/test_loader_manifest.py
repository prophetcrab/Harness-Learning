"""步 5 验收：条目与装配单（loader 起步）。

对应 PLAN.md「M1 拆步」第 5 步：Entry（id / name / config / role / enabled）
+ 路径 id；factory(**config)；装配期 fail loud（含整批回滚）。
"""

from __future__ import annotations

import pytest

from core import Context
from plugins.loader import LOADER_SERVICE, AssemblyError, Entry, LoaderService, loader_plugin

# ---------------------------------------------------------------------------
# 目录：测试用的假插件工厂（name → 工厂；config 就是工厂参数）
# ---------------------------------------------------------------------------


def make_store(label: str = "默认店") -> object:
    class Store:
        name = "store"

        def __init__(self) -> None:
            self.label = label

        def setup(self, ctx):
            ctx.provide("store", self)

    return Store()


def make_greeter(text: str = "你好") -> object:
    class Greeter:
        name = "greeter"

        def setup(self, ctx):
            ctx.provide("greeting", text)

    return Greeter()


def make_waiter() -> object:
    class Waiter:
        name = "waiter"
        inject = ("store",)

        def setup(self, ctx):
            ctx.require("store")

    return Waiter()


def make_boom() -> object:
    class Boom:
        name = "boom"

        def setup(self, ctx):
            ctx.provide("half", 1)
            raise RuntimeError("boom 的 setup 炸了")

    return Boom()


CATALOG = {
    "store": make_store,
    "greeter": make_greeter,
    "waiter": make_waiter,
    "boom": make_boom,
}


def booted() -> tuple[Context, LoaderService]:
    """一个装好 loader 的空世界（启动器在步 8 接管这一步）。"""
    ctx = Context()
    ctx.load_plugin(loader_plugin(CATALOG))
    return ctx, ctx.require(LOADER_SERVICE)


# ---------------------------------------------------------------------------
# 装配：条目 / 路径 / config 注入
# ---------------------------------------------------------------------------


def test_assemble_loads_entries_and_injects_config():
    ctx, loader = booted()
    states = loader.assemble([{"id": "main", "name": "store", "config": {"label": "旗舰店"}}])
    assert ctx.require("store").label == "旗舰店"  # config 进了工厂
    assert [(s.path, s.name, s.status, s.plugin) for s in states] == [
        ("main", "store", "active", "store")
    ]


def test_group_entries_create_path_ids():
    ctx, loader = booted()
    states = loader.assemble(
        [
            {
                "id": "app",
                "children": [
                    {"id": "store", "name": "store"},
                    {"id": "ui", "name": "greeter"},
                ],
            },
        ]
    )
    assert [s.path for s in states] == ["app", "app:store", "app:ui"]
    assert states[0].status == "group" and states[0].plugin is None
    # 装载顺序 = 装配单顺序（分组本身不产生插件）
    assert ctx.active_plugins == ["loader", "store", "greeter"]


def test_entries_may_be_objects_or_dicts():
    ctx, loader = booted()
    loader.assemble(
        [
            Entry(id="obj", name="store", config={"label": "对象形式"}),
            {"id": "dic", "name": "greeter", "config": {"text": "字典形式"}},
        ]
    )
    assert ctx.require("store").label == "对象形式"
    assert ctx.require("greeting") == "字典形式"


def test_entry_copies_config_and_children():
    """装配单是只读真相：外面改源数据不影响已建好的条目。"""
    config = {"label": "原件"}
    children = [Entry(id="kid", name="store")]
    entry = Entry(id="x", name="greeter", config=config, children=children)
    config["label"] = "外面改的"
    children.append(Entry(id="extra", name="store"))
    assert entry.config == {"label": "原件"}
    assert len(entry.children) == 1


def test_disabled_entry_skips_whole_subtree():
    ctx, loader = booted()
    states = loader.assemble(
        [
            {
                "id": "off",
                "enabled": False,
                "children": [{"id": "inner", "name": "store"}],
            },
            {"id": "on", "name": "greeter"},
        ]
    )
    assert [s.status for s in states] == ["skipped", "skipped", "active"]
    assert "store" not in ctx.services
    assert ctx.require("greeting") == "你好"


def test_same_id_under_different_groups_is_fine():
    _ctx, loader = booted()
    states = loader.assemble(
        [
            {"id": "left", "children": [{"id": "store", "name": "store"}]},
            {"id": "right", "children": [{"id": "greeter", "name": "greeter"}]},
        ]
    )
    assert [s.path for s in states] == ["left", "left:store", "right", "right:greeter"]


def test_role_is_carried_but_not_judged():
    """角色词表是启动器的产品政策（0042）：loader 只搬运 role 字符串。"""
    _ctx, loader = booted()
    states = loader.assemble([{"id": "x", "name": "store", "role": "随便什么角色"}])
    assert states[0].role == "随便什么角色"


def test_empty_manifest_is_fine():
    ctx, loader = booted()
    assert loader.assemble([]) == ()
    assert ctx.services == [LOADER_SERVICE]


def test_dependency_order_within_manifest_does_not_matter():
    """装配单顺序只影响台账顺序；激活仍由依赖驱动（步 2 的语义照旧）。"""
    ctx, loader = booted()
    loader.assemble(
        [
            {"id": "w", "name": "waiter"},  # 消费者写在前面
            {"id": "s", "name": "store"},  # 提供者后到
        ]
    )
    assert ctx.require("store").label == "默认店"
    assert {i.name: i.status for i in ctx.plugins}["waiter"] == "active"


# ---------------------------------------------------------------------------
# fail loud：结构
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "store"},  # 缺 id
        {"id": "", "name": "store"},  # 空 id
        {"id": "a:b", "name": "store"},  # id 含分隔符
        {"id": "a b", "name": "store"},  # id 含空白
        {"id": "x", "name": ""},  # 空 name（None 才是分组）
        {"id": "x", "name": "store", "config": "不是字典"},
        {"id": "x", "name": "store", "role": 123},
        {"id": "x", "name": "store", "enabled": "yes"},
        {"id": "x", "name": "store", "confg": {}},  # 字段名写错
    ],
)
def test_malformed_entry_fails_loud(bad):
    ctx, loader = booted()
    with pytest.raises(AssemblyError):
        loader.assemble([bad])
    assert ctx.services == [LOADER_SERVICE]  # 结构问题：什么都没装
    assert loader.snapshot() == ()


def test_duplicate_path_fails_loud():
    ctx, loader = booted()
    with pytest.raises(AssemblyError, match="重复"):
        loader.assemble(
            [
                {"id": "a", "name": "store"},
                {"id": "a", "name": "greeter"},
            ]
        )
    assert ctx.active_plugins == ["loader"]


def test_duplicate_path_across_assembles_fails_loud():
    ctx, loader = booted()
    loader.assemble([{"id": "a", "name": "store"}])
    with pytest.raises(AssemblyError, match="重复"):
        loader.assemble([{"id": "a", "name": "greeter"}])


def test_unknown_name_lists_available():
    _ctx, loader = booted()
    with pytest.raises(AssemblyError) as err:
        loader.assemble([{"id": "x", "name": "不存在"}])
    message = str(err.value)
    assert "不在目录" in message
    assert "store" in message  # 可用名单里提到了目录里有的名字


# ---------------------------------------------------------------------------
# fail loud：构造 / 装载 / 依赖 + 整批回滚
# ---------------------------------------------------------------------------


def test_bad_config_kwargs_fail_loud_with_entry_context():
    ctx, loader = booted()
    with pytest.raises(AssemblyError, match="main"):
        loader.assemble([{"id": "main", "name": "store", "config": {"lable": "错字"}}])
    assert ctx.active_plugins == ["loader"]


def test_two_entries_same_plugin_name_fails_loud_and_rolls_back():
    """一个运行时一个插件名一份实例（0042）：目录名撞上会如实报错、整批回滚。"""
    ctx, loader = booted()
    with pytest.raises(AssemblyError, match="'b'"):
        loader.assemble(
            [
                {"id": "a", "name": "store", "config": {"label": "一号店"}},
                {"id": "b", "name": "store", "config": {"label": "二号店"}},
            ]
        )
    assert ctx.active_plugins == ["loader"]
    assert "store" not in ctx.services


def test_failed_setup_rolls_back_the_whole_batch():
    ctx, loader = booted()
    with pytest.raises(AssemblyError, match="炸了"):
        loader.assemble(
            [
                {"id": "ok", "name": "store"},
                {"id": "bad", "name": "boom"},
            ]
        )
    assert ctx.services == [LOADER_SERVICE]  # ok 也被回滚
    assert ctx.active_plugins == ["loader"]  # 连 failed 记录也不留（本次装载触发的失败）
    assert loader.snapshot() == ()  # 世界回到装配前：本批条目一并摘掉（步 6 语义）


def test_waiter_failure_is_attributed_to_the_waiter():
    """依赖被解开时在**别的**插件里炸：错误要点名真凶，别让人盯着正在装的那条。"""
    ctx, loader = booted()

    class BrokenWaiter:
        name = "broken-waiter"
        inject = ("store",)

        def setup(self, ctx):
            raise RuntimeError("等待者的 setup 炸了")

    ctx.load_plugin(BrokenWaiter())  # 先放一个等待者（不在装配单里）
    with pytest.raises(AssemblyError) as err:
        loader.assemble([{"id": "s", "name": "store"}])
    message = str(err.value)
    assert "broken-waiter" in message  # 真凶被点名
    assert "failed" in message
    assert "store" not in ctx.services  # store 被回滚


def test_pending_residual_fails_loud_and_rolls_back():
    """装配完还有等待依赖 = 装配单缺了提供者：装配期就炸，不留到运行时。"""
    ctx, loader = booted()
    with pytest.raises(AssemblyError, match="等待依赖"):
        loader.assemble([{"id": "w", "name": "waiter"}])  # store 没在单子里
    assert ctx.services == [LOADER_SERVICE]
    assert ctx.pending_plugins == []


# ---------------------------------------------------------------------------
# 快照 / 边界
# ---------------------------------------------------------------------------


def test_snapshot_is_live():
    """快照实时读台账：绕过 loader 直接卸了插件，它会如实说 unloaded。"""
    ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store"}])
    assert loader.snapshot()[0].status == "active"
    ctx.unload_plugin("store")
    assert loader.snapshot()[0].status == "unloaded"


def test_loader_can_be_disabled_and_tree_keeps_running():
    """「装配不是特权」（0042）：停用 loader，树照常跑，只是不能再变。"""
    ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store"}])
    ctx.disable_plugin("loader")
    assert ctx.get(LOADER_SERVICE) is None
    assert ctx.require("store").label == "默认店"  # 树照常运行
