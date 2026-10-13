"""P3 离线模型与交互装配测试：固定回复不限轮数、脚本仍然严格。

两类离线 provider 的分工（见 plugins/llm.py 顶部说明）：
    FakeLLM（script 给了）     —— 严格剧本：用完还调就报错（测试的武器）
    RepeatingLLM（script 没给）—— 固定回复：问不完（界面/长演示）

这条界线值得钉住：交互场景里"离线模型只能用一次"曾是真实缺陷
（浏览器实测时第一次提问就报"剧本已用完"）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from components.llm import text_reply
from plugins import OFFLINE_DEMO_REPLY, boot_offline, offline_llm_plugin


def test_offline_default_repeats_forever(tmp_path: Path):
    """不给 script → 固定回复，连问多轮都答得上（交互演示的核心诉求）。"""
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws")
    for index in range(5):
        result = booted.agent.ask(f"第 {index + 1} 问", session_id="s1")
        assert result.status == "done"
        assert result.final_text == OFFLINE_DEMO_REPLY
    assert booted.agent.open("s1").session.last_turn_number == 5
    booted.close()


def test_offline_script_stays_strict(tmp_path: Path):
    """给了 script → 仍走严格剧本：多调一次模型必须报错（测试纪律不放松）。"""
    booted = boot_offline(
        root=tmp_path / "s",
        workspace=tmp_path / "ws",
        script=[text_reply("只答一次")],
    )
    assert booted.agent.ask("第一问", session_id="s1").final_text == "只答一次"
    with pytest.raises(AssertionError) as excinfo:
        booted.agent.ask("第二问", session_id="s1")
    assert "剧本已用完" in str(excinfo.value)
    booted.close()


def test_provider_choices_map_to_expected_classes(tmp_path: Path):
    """固定回复 provider = RepeatingLLM；显式剧本 = FakeLLM（按类型名断言，
    因为本包的 conftest 会驱逐同名模块，isinstance 跨导入边界不可靠）。"""
    from plugins import fake_llm_plugin

    repeating = offline_llm_plugin("固定")
    assert type(repeating._provider).__name__ == "RepeatingLLM"

    strict = fake_llm_plugin([text_reply("x")])
    assert type(strict._provider).__name__ == "FakeLLM"


def test_repeating_provider_counts_calls():
    """固定回复 provider 也留观测点：calls 计数可用于断言"每步都真的调过模型"。"""
    from components.llm import LLMRequest

    provider = offline_llm_plugin("固定")._provider
    provider.complete(LLMRequest(messages=[]))
    provider.complete(LLMRequest(messages=[]))
    assert provider.calls == 2
