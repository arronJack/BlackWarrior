"""支持 ``python -m blackwarrior``（等价于 ``blackwarrior`` 命令）。

入口脚本已在 pyproject 里注册为 ``blackwarrior``，但补上 ``__main__`` 有两个好处：
  1. 没装包只拿到源码时，`python -m blackwarrior selftest` 依然可用；
  2. Electron 打包态可直接 `python -m blackwarrior.cli serve`（不用依赖 PATH 上的脚本）。
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
