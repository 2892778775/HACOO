"""HACOO 命令行入口 — 任何用户 5 分钟上手。

    python -m hacoo serve [--host H] [--port P]   启动平台服务
    python -m hacoo doctor                        环境自检 (Python/服务/授权/工具/vLLM)
    python -m hacoo demo                          跑一遍 mock 设计流程 (最有直观感受)
    python -m hacoo whoami                        查看当前身份与配额
    python -m hacoo methods [--tool T]            列出可用 api_* 方法
    python -m hacoo instances                     列出我的后端实例
    python -m hacoo call <method> [--instance ID] [--args '{"k":"v"}'] [--async]
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# ------------------------------------------------------------------ serve

def cmd_serve(args: argparse.Namespace) -> int:
    import logging
    from hacoo.config import Config
    from hacoo.server import HacooServer

    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    cfg = Config.from_env()
    server = HacooServer(args.host or cfg.host, args.port or cfg.port)
    print("HACOO server on %s (audit: %s)" % (server.address, cfg.audit_log or "off"))
    try:
        server.serve()
    except KeyboardInterrupt:
        print("\nshutting down ...")
        server.stop()
    return 0


# ------------------------------------------------------------------ doctor

def cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = "", warn: bool = False) -> None:
        nonlocal ok
        mark = "OK " if passed else ("WARN" if warn else "FAIL")
        if not passed and not warn:
            ok = False
        print("[%s] %s%s" % (mark, label, (" — " + detail) if detail else ""))

    # 1) Python 版本
    v = sys.version_info
    check("Python >= 3.8", v >= (3, 8), "%d.%d.%d" % v[:3])

    # 2) hacoo 包可导入
    try:
        import hacoo  # noqa
        check("hacoo package importable", True, "v" + hacoo.__version__)
    except ImportError as exc:
        check("hacoo package importable", False, str(exc))
        return 1

    # 3) 平台服务连通
    from hacoo.access.sdk import HacooClient, HacooClientError
    try:
        client = HacooClient()
        pong = client.ping()["data"]
        check("HACOO server reachable", True,
              "%s (uptime %.0fs)" % (os.environ.get("HACOO_SERVER", "127.0.0.1:9877"),
                                     pong["uptime_s"]))
    except (OSError, HacooClientError) as exc:
        check("HACOO server reachable", False,
              "%s — start it with: python -m hacoo serve" % exc)
        return 1

    # 4) 身份与授权
    try:
        who = client.whoami()
        check("auth / identity", True,
              "user=%s role=%s quota=%d/%d" % (who["user"], who["role"],
                                               who["instances_used"], who["instances_quota"]))
    except HacooClientError as exc:
        check("auth / identity", False, str(exc))
        client.close()
        return 1

    # 5) 已注册工具
    tools = client.list_tools()
    check("registered EDA tools", bool(tools), ", ".join(tools))

    # 6) OpenCode 配置存在
    check("opencode.json present", os.path.isfile("opencode.json"), os.getcwd())

    # 7) vLLM / KIMI (可选, 仅告警)
    base_url = os.environ.get("VLLM_BASE_URL")
    if not base_url:
        check("vLLM endpoint (VLLM_BASE_URL)", False, "not set — needed for OpenCode/agent_runner", warn=True)
    else:
        try:
            import urllib.request
            req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                         headers={"Authorization": "Bearer %s" % os.environ.get("VLLM_API_KEY", "EMPTY")})
            with urllib.request.urlopen(req, timeout=3) as resp:
                check("vLLM endpoint", resp.status == 200, base_url)
        except Exception as exc:
            check("vLLM endpoint", False, "%s: %s" % (base_url, exc), warn=True)

    client.close()
    print("\n%s" % ("all checks passed" if ok else "some checks FAILED — see above"))
    return 0 if ok else 1


# ------------------------------------------------------------------ demo

def cmd_demo(_args: argparse.Namespace) -> int:
    from hacoo.access.sdk import HacooClient

    client = HacooClient()
    print("== HACOO demo: mock timing signoff flow ==\n")
    who = client.whoami()
    print("user: %s (role %s)" % (who["user"], who["role"]))
    with client.session("mock_eda") as eda:
        print("instance: %s" % eda.instance_id)
        print("-> api_load_design(chip.v, top=chip)")
        print("   %s" % eda.call("api_load_design", path="chip.v", top="chip")["text"])
        print("-> api_read_liberty(nangate45.lib)")
        print("   %s" % eda.call("api_read_liberty", path="nangate45.lib")["text"])
        print("-> api_run_synthesis(effort=medium, async)")
        task_id = eda.call("api_run_synthesis", effort="medium", async_=True)["data"]["task_id"]
        task = client.task_status(task_id, wait=True)
        print("   %s" % task["result"]["text"])
        print("-> api_report_timing(max_paths=5)")
        report = eda.call("api_report_timing", max_paths=5)["data"]
        print("   WNS=%.3f ns  TNS=%.3f ns  paths=%d  synthesis_runs=%d"
              % (report["wns"], report["tns"], len(report["paths"]), report["synthesis_runs"]))
        for p in report["paths"][:3]:
            print("     %s  slack=%.3f" % (p["endpoint"], p["slack"]))
    print("\ninstance released. demo done.")
    client.close()
    return 0


# ------------------------------------------------------------------ 查询/调用

def _client():
    from hacoo.access.sdk import HacooClient
    return HacooClient()


def cmd_whoami(_args: argparse.Namespace) -> int:
    c = _client()
    _print_json(c.whoami())
    c.close()
    return 0


def cmd_methods(args: argparse.Namespace) -> int:
    c = _client()
    for m in c.list_methods(tool=args.tool):
        params = ", ".join("%s%s" % (k, "" if v["required"] else "?")
                           for k, v in m["params"].items())
        print("%-24s [%s/%s] %s(%s)" % (m["name"], m["kind"], m["capability"],
                                        m["name"], params))
        if args.verbose and m["description"]:
            print("    %s" % m["description"])
    c.close()
    return 0


def cmd_instances(_args: argparse.Namespace) -> int:
    c = _client()
    instances = c.list_instances()
    if not instances:
        print("(no live instances owned by you)")
    for i in instances:
        print("%s  tool=%s owner=%s alive=%s uptime=%.0fs idle=%.0fs"
              % (i["instance_id"], i["tool"], i["owner"], i["alive"],
                 i["uptime_s"], i["idle_s"]))
    c.close()
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    c = _client()
    call_args = json.loads(args.args) if args.args else {}
    if args.instance:
        result = c.call(args.method, instance_id=args.instance,
                        async_=args.async_, arguments=call_args)
    else:
        result = c.call(args.method, arguments=call_args)
    _print_json(result)
    c.close()
    return 0


# ------------------------------------------------------------------ agents

def cmd_agents(args: argparse.Namespace) -> int:
    from hacoo import agents as registry

    if args.action == "list":
        enabled = set(registry.load_enabled())
        print("%-10s %-10s %-8s %s" % ("name", "stage", "enabled", "title"))
        for spec in sorted(registry.AGENTS.values(), key=lambda s: (s.stage, s.name)):
            print("%-10s %-10s %-8s %s" % (spec.name, spec.stage,
                                           "yes" if spec.name in enabled else "NO",
                                           spec.title))
        print("\nedit: python -m hacoo agents enable|disable <name>; "
              "then 'python -m hacoo agents sync' to regenerate OpenCode agents")
        return 0

    if args.action in ("enable", "disable"):
        if not args.name:
            raise SystemExit("usage: python -m hacoo agents %s <name>" % args.action)
        current = registry.set_enabled(args.name, args.action == "enable")
        print("agent '%s' %sd. enabled: %s" % (args.name, args.action, ", ".join(current)))
        print("run 'python -m hacoo agents sync' to update OpenCode agent files")
        return 0

    if args.action == "sync":
        result = registry.sync_opencode()
        print("written:")
        for p in result["written"]:
            print("  + %s" % p)
        for p in result["removed"]:
            print("  - %s (removed, agent disabled)" % p)
        return 0

    raise SystemExit("unknown agents action: %s" % args.action)


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hacoo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="start the HACOO platform server")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("doctor", help="environment self-check")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("demo", help="run a guided mock design flow")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("whoami", help="show identity and quota")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("methods", help="list registered api_* methods")
    p.add_argument("--tool", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_methods)

    p = sub.add_parser("instances", help="list your live backend instances")
    p.set_defaults(func=cmd_instances)

    p = sub.add_parser("call", help="call an api_* method directly")
    p.add_argument("method")
    p.add_argument("--instance", default=None, help="instance_id for tool methods")
    p.add_argument("--args", default=None, help="JSON object of arguments")
    p.add_argument("--async", dest="async_", action="store_true", help="submit as async task")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("agents", help="manage specialist agents (enable/disable/sync)")
    p.add_argument("action", choices=["list", "enable", "disable", "sync"])
    p.add_argument("name", nargs="?", default=None)
    p.set_defaults(func=cmd_agents)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
