"""webui —— 浏览器界面（作为插件 runs：`plugins/webui.py`）。

本目录只放静态页面（原生 JS、零依赖）；服务端、API、生命周期全在
`plugins/webui.py` 的 `WebUiPlugin` 里——**界面本身就是一个插件**：

    ctx.load_plugin(webui_plugin(port=0))     # 装载：起服务、provide `webui` 服务
    ctx.unload_plugin("webui")                # 卸载：shutdown + 端口释放

页面看三件事：插件台账（谁的 status / inject 是什么）、会话与对话、append-only
事件流（边跑边长）。
"""
