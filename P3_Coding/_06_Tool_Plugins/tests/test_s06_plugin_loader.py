"""`kernel/loader.py` 测试：源码 → 插件工厂（纯机制）。

这条机制从 `plugins/self_tools.py` 提出来（`_06`），因为现在有**两个**消费者：
模型装插件（self-tools）与模型装工具插件（tools）。这组测试钉住它的入口约定与
错误形态——调用方（插件层）负责把它变成给人/给模型的人话。
"""

from __future__ import annotations

import pytest

from kernel import Context
from kernel.loader import plugin_from_source

SIMPLE = '''
class Demo:
    name = "demo"
    def setup(self, ctx):
        ctx.register(lambda: None)

def factory():
    return Demo()
'''

WITHOUT_FACTORY = '''
class OnlyOne:
    name = "only"
    def setup(self, ctx):
        return None
'''


def test_factory_entry_is_preferred():
    plugin, factory, name = plugin_from_source(SIMPLE, path="demo_plugin.py")
    assert name == "demo"
    assert factory().name == "demo"
    assert plugin.setup.__name__ == "setup"


def test_module_named_plugin_function_is_found():
    source = '''
class Widget:
    name = "widget"
    def setup(self, ctx):
        return None

def widget_plugin():
    return Widget()
'''
    _plugin, factory, name = plugin_from_source(source, path="widget.py")
    assert name == "widget"
    assert factory().name == "widget"


def test_single_setup_class_is_the_fallback():
    _plugin, factory, name = plugin_from_source(WITHOUT_FACTORY, path="whatever.py")
    assert name == "only"
    assert factory().name == "only"


def test_explicit_entry_wins():
    source = '''
class A:
    name = "a"
    def setup(self, ctx):
        return None

class B:
    name = "b"
    def setup(self, ctx):
        return None

def factory():
    return A()
'''
    _plugin, _factory, name = plugin_from_source(source, path="x.py", entry="factory")
    assert name == "a"


def test_no_entry_fails_loud():
    with pytest.raises(ValueError) as excinfo:
        plugin_from_source("x = 1\n", path="empty.py")
    assert "找不到插件入口" in str(excinfo.value)
    assert "empty_plugin" in str(excinfo.value)      # 报错里给了可照抄的写法


def test_broken_source_raises_the_real_error():
    """语法/导入错误如实抛出（由调用方转成人话，不在这里吞掉）。"""
    with pytest.raises(Exception) as excinfo:
        plugin_from_source("this is not python !!!", path="bad.py")
    assert excinfo.value is not None


def test_loaded_plugin_is_usable_at_once():
    """装载出来的插件能真的装进 ctx（机制闭环）。"""
    source = '''
class Ready:
    name = "ready"
    def setup(self, ctx):
        ctx.provide("ready", {"ok": True})
'''
    plugin, _factory, _name = plugin_from_source(source, path="ready.py")
    ctx = Context()
    ctx.load_plugin(plugin)
    assert ctx.require("ready") == {"ok": True}


def test_module_names_do_not_collide_across_installs():
    """两次装载同名文件互不干扰（模块名带前缀，且各自是独立对象）。"""
    first, _f1, name1 = plugin_from_source(SIMPLE, path="same.py")
    second, _f2, name2 = plugin_from_source(SIMPLE, path="same.py")
    assert name1 == name2 == "demo"
    assert first is not second


def test_explicit_entry_naming_a_class_instantiates_it():
    """**entry 给类名时要实例化**——这是模型最爱写的形态，实测踩过。

    回归背景：判据原先是 `callable(obj) and not hasattr(obj, "setup")`。
    类对象本身**有** setup 方法（`hasattr(cls, "setup")` 为 True），于是被当成
    "已经是实例"直接交给内核，装载时报
    `setup() missing 1 required positional argument: 'ctx'`——
    这次浏览器实测里，模型写的工具插件正是这个形态（entry="WordCountPlugin"）。
    """
    source = '''
from components.tools import tool


@tool("word_count")
def word_count(text: str) -> dict:
    """统计词数。"""
    return {"words": len(text.split())}


class WordCountPlugin:
    name = "tool-word-count"

    def setup(self, ctx):
        registry = ctx.require("tools")
        registry.register(word_count.definition, word_count.func, owner=self.name)
        return lambda: registry.unregister("word_count")
'''
    plugin, factory, name = plugin_from_source(source, path="word_count.py",
                                               entry="WordCountPlugin")
    assert name == "tool-word-count"
    assert not isinstance(plugin, type), "返回的是类本身，不是实例——装载会炸"

    # 真的能装进 ctx（端到端把那一步走通）
    from components.tools import ToolRegistry

    class Demo:
        name = "tools"

        def setup(self, ctx):
            registry = ToolRegistry()
            ctx.provide("tools", registry)
            return lambda: None

    ctx = Context()
    ctx.load_plugin(Demo())
    ctx.load_plugin(plugin)
    assert "word_count" in ctx.require("tools").names
    assert factory().name == "tool-word-count"   # 工厂也能再造一个
