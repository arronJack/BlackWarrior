"""命令行入口。

    blackwarrior serve          启动本地服务（Electron 壳拉起的也是这个）
    blackwarrior ask "问题"     单轮问答（调试用）
    blackwarrior status         查看运行状态
    blackwarrior selftest       自检
    blackwarrior activate       交互式激活（填 API Key）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, List, Optional


def _build_core(args: Any):
    from .config import load_config
    from .core import WarriorCore

    cfg = load_config()
    if getattr(args, "provider", None):
        cfg.set("provider", args.provider)
    if getattr(args, "model", None):
        cfg.set("model", args.model)
    return WarriorCore(cfg)


def cmd_serve(args: Any) -> int:
    from .server.app import WarriorServer
    from .server.routes import build_routes
    from .version import banner

    core = _build_core(args)
    if getattr(args, "port", None):
        core.config.set("port", int(args.port))
    if getattr(args, "host", None):
        core.config.set("host", args.host)

    server = WarriorServer(core, routes=build_routes())
    # 后台线程跑 serve_forever；主线程留着做 Ctrl+C 收口。
    # （background=False 只 bind 不 accept，服务会"看起来活着但永远不响应"。）
    port = server.start(background=True)
    core.start()

    # 机器可读的就绪标记：Electron 主进程靠这一行拿到真实端口
    # （端口可能被自动 +1 顺延，写死端口在用户机器上迟早会撞）。
    print("[BW-READY] " + json.dumps({
        "url": server.url,
        "port": int(port),
        "data_dir": str(core.paths.data_root()),
        "tier": str(core.kernel.tier),
        "engine": str(core.kernel.engine),
    }, ensure_ascii=False), flush=True)

    print(banner())
    print(f"服务地址：{server.url}")
    print(f"数据目录：{core.paths.data_root()}")
    print(f"认知档位：{core.kernel.tier}（{core.kernel.engine}）"
          + (f" —— {core.kernel.reason}" if core.kernel.reason else ""))
    print(f"已激活：{core.config.is_activated()}")
    print("按 Ctrl+C 停止")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n正在停止…")
        core.close()
    return 0


def cmd_ask(args: Any) -> int:
    core = _build_core(args)
    core.start()
    text = " ".join(args.text)
    if not text.strip():
        print("需要提供问题文本")
        return 1
    result = core.ask(text)
    print(result.text or "(无回复)")
    if result.error:
        print(f"[错误] {result.error}", file=sys.stderr)
    core.close()
    return 0


def cmd_status(args: Any) -> int:
    import urllib.request

    url = (getattr(args, "url", None)
           or f"http://127.0.0.1:{_default_port()}/api/status")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    except Exception as ex:
        print(f"无法连接服务（{url}）：{ex}")
        print("提示：先用 `blackwarrior serve` 启动服务")
        return 1


def _default_port() -> int:
    from .config import load_config

    return int(load_config().get("port", 3721) or 3721)


def cmd_selftest(args: Any) -> int:
    import blackwarrior

    return 0 if blackwarrior.selftest() else 1


def cmd_activate(args: Any) -> int:
    from .config import load_config
    from .llm.providers import list_providers

    cfg = load_config()
    print("可用服务商：")
    for p in list_providers():
        print(f"  {p['key']:<12} {p['name']}")
    print()
    provider = input("选择服务商 [deepseek]: ").strip() or "deepseek"
    key = input("API Key: ").strip()
    model = input("模型名（留空用默认）: ").strip()
    cfg.set("provider", provider)
    if key:
        cfg.set("api_key", key)
    if model:
        cfg.set("model", model)
    cfg.save()
    print(f"已保存到 {cfg._path}")
    return 0


def cmd_tools(args: Any) -> int:
    core = _build_core(args)
    items = core.tools.describe()
    print(f"{'工具':<22} {'风险':<10} {'类别':<12} 说明")
    print("-" * 78)
    for t in items:
        print(f"{t['name']:<22} {t['risk']:<10} {t['category']:<12} "
              f"{t['description'][:40]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="blackwarrior",
        description="黑武士 BlackWarrior —— 带真实认知内核的桌面 AI Agent")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="启动本地服务")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--provider", default=None)
    s.add_argument("--model", default=None)
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("ask", help="单轮问答")
    a.add_argument("text", nargs="*")
    a.add_argument("--provider", default=None)
    a.add_argument("--model", default=None)
    a.set_defaults(func=cmd_ask)

    st = sub.add_parser("status", help="查看运行状态")
    st.add_argument("--url", default=None)
    st.set_defaults(func=cmd_status)

    sub.add_parser("selftest", help="自检").set_defaults(func=cmd_selftest)
    sub.add_parser("activate", help="交互式激活").set_defaults(func=cmd_activate)
    sub.add_parser("tools", help="列出工具").set_defaults(func=cmd_tools)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
