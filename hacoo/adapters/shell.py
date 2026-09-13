"""EDA 工具进程管理助手 — adapter 与真实工具之间的进程桥。

两种模式:
  TclShell    持久交互式 Tcl shell (pt_shell / icc2_shell / innovus -nowin /
              voltus -nowin / redhawk_sc), 命令经 stdin 注入, 用唯一标记
              切分输出; 同一 backend 实例内跨调用保持工具状态 (R6)。
  run_batch   批处理工具 (calibre 等), 每次调用独立子进程, 捕获输出。

工具可执行文件解析顺序: 环境变量 HACOO_<TOOL>_BIN > PATH 中的默认名。
缺失时抛出带配置指引的 RuntimeError (经 backend 规范化为 BACKEND_ERROR)。
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from typing import List, Optional


def resolve_binary(tool: str, default_names: List[str]) -> str:
    """解析工具可执行文件; 找不到时给出明确配置指引。"""
    env_key = "HACOO_%s_BIN" % tool.upper()
    candidate = os.environ.get(env_key)
    if candidate:
        if os.path.isfile(candidate) or shutil.which(candidate):
            return candidate
        raise RuntimeError(
            "%s is set to '%s' but it is not executable; fix the path" % (env_key, candidate))
    for name in default_names:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "%s executable not found (tried: %s). Install the tool or set %s, e.g. "
        "export %s=/tools/synopsys/pt/bin/pt_shell"
        % (tool, ", ".join(default_names), env_key, env_key))


class TclShell:
    """持久 Tcl shell 会话: 后台线程持续读 stdout, 按行入队。"""

    def __init__(self, executable: str, args: Optional[List[str]] = None,
                 startup_timeout: float = 60.0) -> None:
        self.executable = executable
        self.proc = subprocess.Popen(
            [executable] + (args or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._drain_until_idle(startup_timeout)

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def _drain_until_idle(self, timeout: float, idle_s: float = 0.3) -> str:
        """启动横幅等无标记输出: 静默期到达即返回。"""
        out = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                out.append(self._lines.get(timeout=idle_s))
            except queue.Empty:
                break
        return "".join(out)

    def cmd(self, command: str, timeout: float = 300.0) -> str:
        """发送一条 Tcl 命令, 返回到结束标记为止的全部输出。"""
        if self.proc.poll() is not None:
            raise RuntimeError("%s shell exited (rc=%s)" % (self.executable, self.proc.returncode))
        marker = "__HACOO_EOM_%s__" % uuid.uuid4().hex[:8]
        assert self.proc.stdin is not None
        self.proc.stdin.write("%s; puts %s\n" % (command.rstrip(";"), marker))
        self.proc.stdin.flush()
        out: List[str] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self._lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if line.strip() == marker:
                return "".join(out)
            # 跳过命令回显中混入的标记行
            if marker in line and line.strip().startswith("puts"):
                continue
            out.append(line)
        raise RuntimeError("command timed out after %.0fs: %s" % (timeout, command))

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()


def run_batch(argv: List[str], timeout: float = 3600.0,
              cwd: Optional[str] = None) -> str:
    """批处理执行 (calibre 等): 返回合并的 stdout+stderr。"""
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, cwd=cwd)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError("batch command failed (rc=%d): %s\n%s"
                           % (proc.returncode, " ".join(argv), output[-2000:]))
    return output
