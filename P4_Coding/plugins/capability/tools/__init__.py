"""tools 能力插件组：六件（组）具体工具。

    math.py         tool-math            calc / math
    fs.py           tool-fs              read_file / list_files / write_file / edit_file
    shell.py        tool-shell           shell
    fs_search.py    tool-fs-search       glob / grep
    web_search.py   tool-web-search      web_fetch / web_search
    ask_user.py     tool-ask-user        ask_user

它们都只依赖 `tool-admin`（注册表 + 管线）与各自需要的接缝（fs / proc /
workspace / prompt）——不认识彼此。
"""
