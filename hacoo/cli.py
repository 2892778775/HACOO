"""HACOO 命令行入口 — 任何用户 5 分钟上手。

    python -m hacoo serve [--host H] [--port P]   启动平台服务
    python -m hacoo dashboard [--port 8501]       启动可视化操作平台 (可选依赖 streamlit)
    python -m hacoo doctor                        环境自检 (Python/服务/授权/工具/vLLM)
    python -m hacoo demo                          跑一遍 mock 设计流程 (最有直观感受)
    python -m hacoo whoami                        查看当前身份与配额
    python -m hacoo methods [--tool T]            列出可用 api_* 方法
    python -m hacoo instances                     列出我的后端实例
    python -m hacoo runs [--status success] [--limit 20]   列出 flow run 摘要
    python -m hacoo call <method> [--instance ID] [--args '{"k":"v"}'] [--async]
    python -m hacoo feedback --agent sta --content "..." [--decision-id dec-xxx]
    python -m hacoo review [--agent sta]         待复核队列 (决策 + 知识条目)
    python -m hacoo review-mark --entry kb-xxx --action approve [--note "..."]
    python -m hacoo insights [--agent sta] [--scan] [--kind self_issue]
    python -m hacoo insight-mark --id ins-xxx --action confirm [--create-upgrade]
    python -m hacoo upgrade propose --kind agent_capability --target sta --title "..." \
        --motivation "..." [--acceptance "bench case sta-mcmm-01 通过"]
    python -m hacoo upgrade list [--status proposed] [--target sta]
    python -m hacoo upgrade update --id up-xxx --to accepted [--version-after sta@1.3.0]
    python -m hacoo agents version sta 1.3.0     版本 bump (写 hacoo.agents.json)
    python -m hacoo compat snapshot|check        契约快照 (重建 / 只校验)
    python -m hacoo bench run [--agent sta] [--tag T]
    python -m hacoo bench save-baseline --agent sta
    python -m hacoo bench compare --baseline baselines/sta@1.2.0.json
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


# ------------------------------------------------------------------ dashboard

def cmd_dashboard(args: argparse.Namespace) -> int:
    """启动 Streamlit 可视化操作平台 (SPEC-dashboard §1.3)。

    streamlit 为可选依赖: 优先用当前解释器 ([sys.executable, -m, streamlit]),
    找不到时回退 PATH 上的 streamlit, 再不行给出中文安装引导。
    """
    import importlib.util
    import shutil
    import subprocess

    port = args.port or int(os.environ.get("HACOO_DASH_PORT", "8501"))
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "dashboard", "app.py")
    if importlib.util.find_spec("streamlit") is not None:
        cmd = [sys.executable, "-m", "streamlit", "run", script,
               "--server.port", str(port)]
    elif shutil.which("streamlit"):
        cmd = ["streamlit", "run", script, "--server.port", str(port)]
    else:
        print("未找到 streamlit（Dashboard 为可选组件，核心层不依赖它）。\n"
              "请安装后重试（streamlit 需要 Python >= 3.9）：\n"
              "    pip install streamlit\n"
              "然后: python -m hacoo dashboard [--port %d]" % port)
        return 1
    print("HACOO dashboard: http://127.0.0.1:%d  (Ctrl+C 停止)" % port)
    return subprocess.call(cmd)


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


def cmd_runs(args: argparse.Namespace) -> int:
    c = _client()
    runs = c.runs_list(limit=args.limit, status=args.status)
    if not runs:
        print("(no flow runs)")
    print("%-14s %-14s %-9s %-10s %s" % ("run_id", "template", "status",
                                         "user", "duration_s"))
    for r in runs:
        print("%-14s %-14s %-9s %-10s %s"
              % (r["run_id"], r["template"], r["status"], r["user"],
                 r["duration_s"] if r["duration_s"] is not None else "-"))
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
        versions = registry.load_versions()
        print("%-10s %-10s %-8s %-8s %s" % ("name", "stage", "version", "enabled", "title"))
        for spec in sorted(registry.AGENTS.values(), key=lambda s: (s.stage, s.name)):
            print("%-10s %-10s %-8s %-8s %s"
                  % (spec.name, spec.stage, versions.get(spec.name, spec.version),
                     "yes" if spec.name in enabled else "NO", spec.title))
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

    if args.action == "version":
        if not args.name or not args.value:
            raise SystemExit("usage: python -m hacoo agents version <name> <x.y.z>")
        versions = registry.set_version(args.name, args.value)
        print("agent '%s' version -> %s (versions: %s)"
              % (args.name, args.value,
                 ", ".join("%s@%s" % kv for kv in sorted(versions.items()))))
        print("run 'python -m hacoo agents sync' to update OpenCode agent files")
        return 0

    raise SystemExit("unknown agents action: %s" % args.action)


# ------------------------------------------------------------------ 契约快照 (Part B.2)

def cmd_compat(args: argparse.Namespace) -> int:
    from hacoo import compat

    if args.compat_action == "snapshot":
        for path in compat.write_snapshots():
            print("snapshot written: %s" % path)
        return 0
    # check: 只校验不更新; 破坏性变更退出码 1, 宽松方向变化仅提示
    result = compat.check_snapshots(update_additions=False)
    for line in result["breaking"]:
        print("BREAKING: %s" % line)
    for line in result["additions"]:
        print("addition: %s" % line)
    if result["breaking"]:
        print("\ncontract check FAILED — breaking changes must be deliberate: "
              "update the snapshot and declare BREAKING in the commit message")
        return 1
    if result["additions"]:
        print("\nnon-breaking additions detected — run 'python -m hacoo compat snapshot' "
              "to refresh the committed snapshots")
    else:
        print("contract check OK — snapshots match")
    return 0


# ------------------------------------------------------------------ 升级提案 (Part A)

def cmd_upgrade(args: argparse.Namespace) -> int:
    c = _client()
    if args.upgrade_action == "propose":
        entry = c.upgrade_propose(
            title=args.title, kind=args.kind, target=args.target,
            motivation=args.motivation, acceptance=args.acceptance or "",
            proposed_change=args.proposed_change or "",
            source=args.source, source_ref=args.source_ref)
        print("proposal %s created (status=%s)" % (entry["proposal_id"], entry["status"]))
        _print_json(entry)
    elif args.upgrade_action == "list":
        _print_json(c.upgrade_list(status=args.status, target=args.target,
                                   limit=args.limit))
    elif args.upgrade_action == "update":
        entry = c.upgrade_update(args.id, args.to, note=args.note or "",
                                 version_after=args.version_after)
        print("proposal %s -> %s%s" % (entry["proposal_id"], entry["status"],
                                       " (version_after=%s)" % entry["version_after"]
                                       if entry.get("version_after") else ""))
    c.close()
    return 0


# ------------------------------------------------------------------ 成长闭环

def cmd_feedback(args: argparse.Namespace) -> int:
    c = _client()
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    entry = c.feedback(agent=args.agent, content=args.content,
                       decision_id=args.decision_id,
                       feedback_type=args.feedback_type,
                       tags=tags or None, run_id=args.run_id)
    print("feedback recorded: %s (source=%s, weight=%s)"
          % (entry["id"], entry["source"], entry["weight"]))
    _print_json(entry)
    c.close()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    c = _client()
    queue = c.review_queue(agent=args.agent, limit=args.limit)
    print("pending decisions: %d, unreviewed knowledge: %d"
          % (len(queue["decisions"]), len(queue["knowledge"])))
    _print_json(queue)
    c.close()
    return 0


def cmd_review_mark(args: argparse.Namespace) -> int:
    c = _client()
    entry = c.knowledge_review(args.entry, args.action, note=args.note or "")
    print("knowledge %s -> %s (weight=%s)"
          % (entry["id"], entry["review_status"], entry["weight"]))
    c.close()
    return 0


def cmd_insights(args: argparse.Namespace) -> int:
    c = _client()
    if args.scan:
        result = c.insights_scan(agent=args.agent, window_days=args.window_days)
        print("scan: %d new insight(s), %d skipped (already known)"
              % (len(result["created"]), result["skipped"]))
        for ins in result["created"]:
            print("  [%s/%s] %s — %s" % (ins["insight_id"], ins["kind"],
                                         ins["agent"], ins["title"]))
    else:
        _print_json(c.insights_list(agent=args.agent, kind=args.kind,
                                    status=args.status, limit=args.limit))
    c.close()
    return 0


def cmd_insight_mark(args: argparse.Namespace) -> int:
    c = _client()
    data = c.insight_review(args.id, args.action, note=args.note or "",
                            auto_kb=not args.no_kb, create_upgrade=args.create_upgrade)
    print("insight %s -> %s%s%s" % (
        data["insight_id"], data["status"],
        " (kb entry %s)" % data["kb_entry_id"] if data.get("kb_entry_id") else "",
        " (upgrade %s)" % data["upgrade_proposal_id"]
        if data.get("upgrade_proposal_id") else ""))
    c.close()
    return 0


# ------------------------------------------------------------------ 能力基准 (Part B.3)

def cmd_bench(args: argparse.Namespace) -> int:
    from hacoo.bench import BenchRunner

    runner = BenchRunner()
    if args.bench_action == "run":
        report = runner.run_all(agent=args.agent, tags=args.tag)
        for r in report["cases"]:
            if r.get("skipped"):
                print("[SKIP] %s (%.2fs) — %s"
                      % (r["id"], r["duration_s"], r.get("skip_reason", "")))
                continue
            mark = "PASS" if r["pass"] else "FAIL"
            print("[%s] %s (%.2fs)%s" % (mark, r["id"], r["duration_s"],
                                         "" if r["pass"] else " — " + "; ".join(r["failures"])))
        skipped_note = (" — skipped: %d" % report["skipped"]
                        if report.get("skipped") else "")
        print("\npass rate: %d/%d (%.0f%%)%s — run file: %s"
              % (report["passed"], report["total"], report["pass_rate"] * 100,
                 skipped_note, report.get("run_file", "-")))
        return 0 if report["failed"] == 0 else 1
    if args.bench_action == "save-baseline":
        if not args.agent:
            raise SystemExit("usage: python -m hacoo bench save-baseline --agent <name>")
        path = runner.save_baseline(args.agent)
        print("baseline saved: %s" % path)
        return 0
    if args.bench_action == "compare":
        if not args.baseline:
            raise SystemExit("usage: python -m hacoo bench compare --baseline <path>")
        result = runner.compare(args.baseline)
        print("agent=%s baseline=%s pass_rate: %.0f%% -> %.0f%% (delta %+.0f%%)"
              % (result["agent"], result["baseline"],
                 (result["baseline_pass_rate"] or 0) * 100,
                 result["current_pass_rate"] * 100, result["pass_rate_delta"] * 100))
        print("fail->pass: %s" % (", ".join(result["fail_to_pass"]) or "(none)"))
        print("pass->fail: %s" % (", ".join(result["pass_to_fail"]) or "(none)"))
        if result["new_cases"]:
            print("new cases: %s" % ", ".join(result["new_cases"]))
        if result["pass_to_fail"]:
            print("\nREGRESSION: pass->fail cases must be fixed before marking "
                  "an upgrade done (B.3.4)")
            return 1
        return 0
    raise SystemExit("unknown bench action: %s" % args.bench_action)


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

    p = sub.add_parser("dashboard", help="start the Streamlit web dashboard "
                       "(optional dependency: streamlit, Python >= 3.9)")
    p.add_argument("--port", type=int, default=None,
                   help="default: HACOO_DASH_PORT or 8501")
    p.set_defaults(func=cmd_dashboard)

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

    p = sub.add_parser("runs", help="list flow run summaries (latest first)")
    p.add_argument("--status", default=None,
                   choices=["running", "success", "failed"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("call", help="call an api_* method directly")
    p.add_argument("method")
    p.add_argument("--instance", default=None, help="instance_id for tool methods")
    p.add_argument("--args", default=None, help="JSON object of arguments")
    p.add_argument("--async", dest="async_", action="store_true", help="submit as async task")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("agents", help="manage specialist agents (enable/disable/sync/version)")
    p.add_argument("action", choices=["list", "enable", "disable", "sync", "version"])
    p.add_argument("name", nargs="?", default=None)
    p.add_argument("value", nargs="?", default=None, help="version number for 'version' action")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("feedback", help="submit human feedback about an agent's decision "
                       "(correction lands in the knowledge base with high weight)")
    p.add_argument("--agent", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--decision-id", default=None)
    p.add_argument("--feedback-type", default="correction",
                   choices=["correction", "confirmation", "note"])
    p.add_argument("--tags", default=None, help="comma-separated tags")
    p.add_argument("--run-id", default=None)
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("review", help="show the human review queue "
                       "(pending decisions + unreviewed knowledge entries)")
    p.add_argument("--agent", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("review-mark", help="approve/reject a knowledge entry")
    p.add_argument("--entry", required=True, help="knowledge entry id (kb-xxx)")
    p.add_argument("--action", required=True, choices=["approve", "reject"])
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_review_mark)

    p = sub.add_parser("insights", help="list insights, or run detectors with --scan")
    p.add_argument("--agent", default=None)
    p.add_argument("--scan", action="store_true", help="run detectors and persist new insights")
    p.add_argument("--kind", default=None,
                   choices=["anomaly", "trend", "blind_spot", "self_issue"])
    p.add_argument("--status", default=None, choices=["new", "confirmed", "dismissed"])
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--window-days", type=int, default=7)
    p.set_defaults(func=cmd_insights)

    p = sub.add_parser("insight-mark", help="confirm/dismiss an insight "
                       "(confirm auto-creates a knowledge entry unless --no-kb)")
    p.add_argument("--id", required=True, help="insight id (ins-xxx)")
    p.add_argument("--action", required=True, choices=["confirm", "dismiss"])
    p.add_argument("--note", default=None)
    p.add_argument("--no-kb", action="store_true", help="do not auto-create knowledge entry")
    p.add_argument("--create-upgrade", action="store_true",
                   help="on confirm, also open an upgrade proposal (source=insight)")
    p.set_defaults(func=cmd_insight_mark)

    p = sub.add_parser("upgrade", help="manage upgrade proposals (SPEC-evolution Part A)")
    p.add_argument("upgrade_action", choices=["propose", "list", "update"])
    p.add_argument("--title", default=None)
    p.add_argument("--kind", default=None,
                   choices=["agent_capability", "skill_new", "skill_update",
                            "knowledge_bulk", "adapter_method", "platform_feature"])
    p.add_argument("--target", default=None, help="agent/skill/tool name or 'platform'")
    p.add_argument("--motivation", default=None)
    p.add_argument("--acceptance", default=None,
                   help="verifiable acceptance criteria (required before accept)")
    p.add_argument("--proposed-change", default=None)
    p.add_argument("--source", default="human", choices=["human", "insight", "review"])
    p.add_argument("--source-ref", default=None)
    p.add_argument("--status", default=None,
                   choices=["proposed", "accepted", "in_progress", "done", "rejected"])
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--id", default=None, help="proposal id (up-xxx) for update")
    p.add_argument("--to", default=None,
                   choices=["accepted", "in_progress", "done", "rejected"])
    p.add_argument("--note", default=None)
    p.add_argument("--version-after", default=None, help="e.g. sta@1.3.0 (recorded on done)")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("compat", help="contract snapshots: rebuild or verify "
                       "(tests/contracts/)")
    p.add_argument("compat_action", choices=["snapshot", "check"])
    p.set_defaults(func=cmd_compat)

    p = sub.add_parser("bench", help="capability benchmarks: golden cases / baselines")
    p.add_argument("bench_action", choices=["run", "save-baseline", "compare"])
    p.add_argument("--agent", default=None, help="only run cases of this agent")
    p.add_argument("--tag", action="append", default=None,
                   help="only run cases with this tag (repeatable)")
    p.add_argument("--baseline", default=None, help="baseline file for compare")
    p.set_defaults(func=cmd_bench)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
