"""Configurable GUI launcher for the local Soul Tide server stack.

The executable intentionally contains no project or toolchain absolute path.
Choose the server directory once, enter an ADB serial/port, and the launcher
delegates lifecycle work to the repository's existing PowerShell scripts.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox
from tkinter import END, DISABLED, NORMAL
from tkinter import ttk


APP_NAME = "SoulTideLocalServerLauncher"
CONFIG_FILE_NAME = "settings.json"
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
DISCOVERY_TIMEOUT_SECONDS = 8.0
SCRIPT_NAMES = {
    "start": "start_stack.ps1",
    "stop": "stop_stack.ps1",
    "restart": "start_stack.ps1",
    "status": "status_stack.ps1",
}


def _is_server_root(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in SCRIPT_NAMES.values())


def _iter_parent_dirs(path: Path):
    current = path.resolve()
    while True:
        yield current
        if current.parent == current:
            return
        current = current.parent


def _find_named_file(root: Path, names: set[str], deadline: float) -> Path | None:
    """Find the first named file below root without an unbounded scan."""
    if time.monotonic() >= deadline:
        return None
    try:
        for current, directories, files in os.walk(root, topdown=True):
            if time.monotonic() >= deadline:
                return None
            directories[:] = [name for name in directories if name not in {".git", "__pycache__", "launcher_output"}]
            for file_name in files:
                if file_name.lower() in names:
                    return Path(current) / file_name
    except OSError:
        return None
    return None


def discover_tools(search_root: Path | None = None, timeout: float = DISCOVERY_TIMEOUT_SECONDS) -> dict[str, str | bool]:
    """Discover colocated server scripts, adb, and PowerShell for the GUI.

    Parent directories are checked first so an EXE in launcher_output finds its
    repository immediately. Only then is the EXE directory scanned recursively.
    The bounded scan keeps startup responsive when the EXE is placed near a
    large resource tree.
    """
    anchor = (search_root or Path(sys.executable).resolve().parent).resolve()
    deadline = time.monotonic() + max(0.1, timeout)
    result: dict[str, str | bool] = {"serverRoot": "", "adbPath": "", "shellPath": "", "timedOut": False}

    for parent in _iter_parent_dirs(anchor):
        if _is_server_root(parent):
            result["serverRoot"] = str(parent)
            break

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell:
        result["shellPath"] = str(Path(shell).resolve())

    if not result["serverRoot"] and time.monotonic() < deadline:
        server_script = _find_named_file(anchor, {"start_stack.ps1"}, deadline)
        if server_script:
            candidate = server_script.parent
            if _is_server_root(candidate):
                result["serverRoot"] = str(candidate.resolve())

    adb_roots = [anchor]
    if result["serverRoot"]:
        server_root = Path(str(result["serverRoot"]))
        if server_root not in adb_roots:
            adb_roots.insert(0, server_root)
    for adb_root in adb_roots:
        local_adb = _find_named_file(adb_root, {"adb.exe", "adb"}, deadline)
        if local_adb:
            result["adbPath"] = str(local_adb.resolve())
            break

    if not result["adbPath"]:
        adb = shutil.which("adb")
        if adb:
            result["adbPath"] = str(Path(adb).resolve())

    result["timedOut"] = time.monotonic() >= deadline
    return result


def config_path() -> Path:
    app_data = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    base = Path(app_data) if app_data else Path.home() / ".config"
    return base / APP_NAME / CONFIG_FILE_NAME


def resolve_powershell(explicit: str = "") -> str:
    candidates = [explicit.strip()] if explicit.strip() else []
    candidates.extend(["pwsh", "powershell", "PowerShell"])
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path.resolve())
    raise FileNotFoundError("未找到 PowerShell，请在设置中填写 powershell.exe 或 pwsh 路径")


def normalize_adb_serial(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.isdigit():
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError("ADB 端口必须在 1-65535 之间")
        return f"127.0.0.1:{port}"
    if not SERIAL_PATTERN.fullmatch(value):
        raise ValueError("ADB 序列号/端口包含无效字符")
    if ":" in value:
        host, port_text = value.rsplit(":", 1)
        if not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ValueError("ADB 地址应为 host:port，端口必须在 1-65535 之间")
    return value


def validate_server_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"服务器目录不存在：{root}")
    missing = [name for name in ("start_stack.ps1", "stop_stack.ps1", "status_stack.ps1") if not (root / name).is_file()]
    if missing:
        raise ValueError("服务器目录缺少脚本：" + ", ".join(missing))
    return root


def load_settings() -> dict[str, object]:
    path = config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_settings(value: dict[str, object]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class LauncherApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("灵魂潮汐本地服务器启动器")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.worker_running = False
        self.worker_process: subprocess.Popen[str] | None = None

        settings = load_settings()
        discovered = discover_tools()
        saved_server = str(settings.get("serverRoot", ""))
        saved_adb = str(settings.get("adbPath", ""))
        saved_shell = str(settings.get("shellPath", ""))
        saved_server_path = Path(saved_server).expanduser() if saved_server else None
        saved_adb_path = Path(saved_adb).expanduser() if saved_adb else None
        saved_shell_path = Path(saved_shell).expanduser() if saved_shell else None
        server_value = saved_server if saved_server_path and _is_server_root(saved_server_path) else str(discovered["serverRoot"])
        adb_value = saved_adb if saved_adb_path and saved_adb_path.is_file() else str(discovered["adbPath"])
        shell_value = saved_shell if saved_shell_path and saved_shell_path.is_file() else str(discovered["shellPath"])
        self.server_root = StringVar(value=server_value)
        self.adb_serial = StringVar(value=str(settings.get("adbSerial", "")))
        self.adb_path = StringVar(value=adb_value)
        self.package = StringVar(value=str(settings.get("package", "com.glkj.lhcx.aligames")))
        self.shell_path = StringVar(value=shell_value)
        self.skip_routes = BooleanVar(value=bool(settings.get("skipRoutes", False)))
        discovery_note = "已自动发现工具" if any(discovered[key] for key in ("serverRoot", "adbPath", "shellPath")) else "未自动发现工具，请手动选择"
        if discovered["timedOut"]:
            discovery_note += "（搜索已到时限）"
        self.status_text = StringVar(value=discovery_note)
        self.buttons: list[ttk.Button] = []
        self.build_ui()
        self.root.after(100, self.flush_output)

    def build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

        ttk.Label(frame, text="服务器目录").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=5)
        ttk.Entry(frame, textvariable=self.server_root).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(frame, text="浏览...", command=self.choose_server_root).grid(row=0, column=2, padx=(8, 0), pady=5)

        ttk.Label(frame, text="ADB 端口/序列号").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=5)
        ttk.Entry(frame, textvariable=self.adb_serial).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="可填 16416、127.0.0.1:16416 或 emulator-5556").grid(row=1, column=2, sticky="w", padx=(8, 0), pady=5)

        ttk.Label(frame, text="ADB 工具路径").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=5)
        ttk.Entry(frame, textvariable=self.adb_path).grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Button(frame, text="浏览...", command=self.choose_adb).grid(row=2, column=2, padx=(8, 0), pady=5)

        ttk.Label(frame, text="客户端包名").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=5)
        ttk.Entry(frame, textvariable=self.package).grid(row=3, column=1, sticky="ew", pady=5)

        ttk.Label(frame, text="PowerShell 路径").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=5)
        ttk.Entry(frame, textvariable=self.shell_path).grid(row=4, column=1, sticky="ew", pady=5)
        ttk.Button(frame, text="浏览...", command=self.choose_shell).grid(row=4, column=2, padx=(8, 0), pady=5)

        ttk.Checkbutton(frame, text="跳过 MuMu 路由检查（仅在不需要修复模拟器路由时使用）", variable=self.skip_routes).grid(row=5, column=1, columnspan=2, sticky="w", pady=5)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        ttk.Button(button_frame, text="自动查找工具", command=self.rediscover_tools).pack(side="left", padx=(0, 12))
        for label, action in (("启动", "start"), ("关闭", "stop"), ("重启", "restart"), ("检查状态", "status")):
            button = ttk.Button(button_frame, text=label, command=lambda name=action: self.run_action(name))
            button.pack(side="left", padx=(0, 8))
            self.buttons.append(button)
        ttk.Label(button_frame, textvariable=self.status_text).pack(side="right")

        output_frame = ttk.Frame(frame)
        output_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        self.output = tk_text = tk.Text(output_frame, wrap="none", state=DISABLED)
        scroll = ttk.Scrollbar(output_frame, orient="vertical", command=tk_text.yview)
        tk_text.configure(yscrollcommand=scroll.set)
        tk_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def choose_server_root(self) -> None:
        selected = filedialog.askdirectory(title="选择服务器目录")
        if selected:
            self.server_root.set(selected)

    def choose_shell(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 PowerShell 可执行文件",
            filetypes=(("PowerShell", "*.exe"), ("所有文件", "*.*")),
        )
        if selected:
            self.shell_path.set(selected)

    def choose_adb(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 adb.exe",
            filetypes=(("ADB", "adb.exe"), ("所有文件", "*.*")),
        )
        if selected:
            self.adb_path.set(selected)

    def rediscover_tools(self) -> None:
        discovered = discover_tools()
        for variable, key in ((self.server_root, "serverRoot"), (self.adb_path, "adbPath"), (self.shell_path, "shellPath")):
            if discovered[key]:
                variable.set(str(discovered[key]))
        note = "已完成自动查找"
        if discovered["timedOut"]:
            note += "（搜索已到时限，未找到的项目请手动选择）"
        self.status_text.set(note)

    def append_output(self, text: str) -> None:
        self.output.configure(state=NORMAL)
        self.output.insert(END, text)
        self.output.see(END)
        self.output.configure(state=DISABLED)

    def flush_output(self) -> None:
        try:
            while True:
                self.append_output(self.output_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.flush_output)

    def collect_options(self) -> tuple[Path, str, str, str, bool, str]:
        server_root = validate_server_root(self.server_root.get())
        serial = normalize_adb_serial(self.adb_serial.get())
        adb_path = self.adb_path.get().strip()
        if adb_path:
            adb_file = Path(adb_path).expanduser().resolve()
            if not adb_file.is_file():
                raise ValueError(f"ADB 工具不存在：{adb_file}")
            adb_path = str(adb_file)
        elif not self.skip_routes.get():
            adb_path = shutil.which("adb") or ""
            if not adb_path:
                raise ValueError("未找到 adb.exe，请手动填写 ADB 工具路径，或勾选跳过 MuMu 路由检查")
        package = self.package.get().strip()
        if package and not SERIAL_PATTERN.fullmatch(package):
            raise ValueError("客户端包名包含无效字符")
        shell = resolve_powershell(self.shell_path.get())
        return server_root, serial, package, shell, bool(self.skip_routes.get()), adb_path

    def save_current_options(self, server_root: Path, serial: str, package: str, shell: str, skip_routes: bool, adb_path: str) -> None:
        save_settings({
            "serverRoot": str(server_root),
            "adbSerial": serial,
            "package": package,
            "shellPath": shell,
            "skipRoutes": skip_routes,
            "adbPath": adb_path,
        })

    def command_for(self, action: str, options: tuple[Path, str, str, str, bool, str]) -> list[str]:
        server_root, serial, package, shell, skip_routes, adb_path = options
        script_name = SCRIPT_NAMES[action]
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(server_root / script_name)]
        if action in {"start", "status"}:
            if serial:
                command.extend(["-AdbSerial", serial])
            if package:
                command.extend(["-Package", package])
            if adb_path:
                command.extend(["-MumuAdb", adb_path])
            if skip_routes:
                command.append("-SkipMumuRoutes")
        return command

    def run_action(self, action: str) -> None:
        if self.worker_running:
            return
        try:
            options = self.collect_options()
            self.save_current_options(*options)
        except (OSError, ValueError) as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.worker_running = True
        self.set_buttons(DISABLED)
        self.status_text.set({"start": "正在启动", "stop": "正在关闭", "restart": "正在重启", "status": "正在检查"}[action])
        threading.Thread(target=self.worker, args=(action, options), daemon=True).start()

    def worker(self, action: str, options: tuple[Path, str, str, str, bool, str]) -> None:
        try:
            actions = ["stop", "start"] if action == "restart" else [action]
            final_code = 0
            for current in actions:
                command = self.command_for(current, options)
                self.output_queue.put("\n> " + " ".join(self.display_arg(item) for item in command) + "\n")
                final_code = self.run_process(command, options[0])
                if final_code != 0:
                    break
            result = "完成" if final_code == 0 else f"失败（退出码 {final_code}）"
            self.output_queue.put(f"\n{result}\n")
            self.root.after(0, lambda: self.status_text.set(result))
        except Exception as exc:
            self.output_queue.put(f"\n启动器异常：{exc}\n")
            self.root.after(0, lambda: self.status_text.set("执行异常"))
        finally:
            self.worker_process = None
            self.worker_running = False
            self.root.after(0, lambda: self.set_buttons(NORMAL))

    @staticmethod
    def display_arg(value: str) -> str:
        return f'"{value}"' if " " in value else value

    def run_process(self, command: list[str], cwd: Path) -> int:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
        self.worker_process = process
        assert process.stdout is not None
        for line in process.stdout:
            self.output_queue.put(line)
        return process.wait()

    def set_buttons(self, state: str) -> None:
        for button in self.buttons:
            button.configure(state=state)

    def close(self) -> None:
        if self.worker_running:
            messagebox.showwarning("操作进行中", "请等待当前服务器操作完成")
            return
        self.root.destroy()


def main() -> int:
    root = Tk()
    LauncherApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
