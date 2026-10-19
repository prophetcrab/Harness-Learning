"""proc 能力插件组：命令执行后端。

    backends.py  proc-local-git-bash / proc-local-posix / proc-scripted

三个都是"把执行实现注册进 proc 服务"的薄插件；选哪个由平台与优先级决定。
"""
