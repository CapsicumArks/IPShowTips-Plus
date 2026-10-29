"""网卡 IP 配置切换模块（集成自 IPSwitch.bat）。

功能：DHCP 自动获取、静态 IP 预设/自定义配置、查看当前配置。
静态配置需管理员权限，通过 UAC 提权执行 netsh。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, ttk

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PRESETS_FILE = "ip_presets.json"

# 内置示例预设（仅作兜底，真实预设请写入 ip_presets.json，不随仓库发布）
DEFAULT_PRESETS = [
    {
        "name": "示例-办公 192.168.10.x",
        "base": "192.168.10",
        "mask": "255.255.255.0",
        "gateway": "192.168.10.254",
        "dns1": "192.168.10.1",
        "dns2": "223.5.5.5",
    },
    {
        "name": "示例-家庭 192.168.0.x",
        "base": "192.168.0",
        "mask": "255.255.255.0",
        "gateway": "192.168.0.1",
        "dns1": "114.114.114.114",
        "dns2": "8.8.8.8",
    },
    {
        "name": "示例-开发 10.0.0.x",
        "base": "10.0.0",
        "mask": "255.0.0.0",
        "gateway": "10.0.0.1",
        "dns1": "10.0.0.1",
        "dns2": "8.8.8.8",
    },
]


def _preset_search_dirs():
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    dirs.append(os.getcwd())
    return dirs


def load_presets():
    """加载 ip_presets.json（exe/脚本同目录优先），失败则用内置示例。"""
    for d in _preset_search_dirs():
        path = os.path.join(d, PRESETS_FILE)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            raw = data["presets"] if isinstance(data, dict) else data
            presets = []
            for p in raw:
                if not all(k in p for k in ("base", "mask", "gateway", "dns1")):
                    continue
                presets.append(
                    {
                        "name": str(p.get("name", "未命名")),
                        "base": p["base"],
                        "mask": p["mask"],
                        "gateway": p["gateway"],
                        "dns1": p["dns1"],
                        "dns2": p.get("dns2", ""),
                    }
                )
            if presets:
                return presets
        except Exception:
            pass
    return DEFAULT_PRESETS


# ---------------------------------------------------------------- 基础能力

def _run_powershell(cmd, timeout=30):
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    return proc.stdout.strip()


_VIRTUAL_NIC_RE = "VMware|Virtual|Loopback|vEthernet|WSL|Docker|Hyper-V"


def _default_route_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def list_local_ips():
    """当前生效的内网 IPv4（10.* / 192.*），默认路由优先，排除虚拟网卡。"""
    ips = []
    try:
        out = _run_powershell(
            "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {"
            "($_.IPAddress -like '10.*' -or $_.IPAddress -like '192.*') -and "
            f"$_.InterfaceAlias -notmatch '{_VIRTUAL_NIC_RE}'}} "
            "| Select-Object -ExpandProperty IPAddress"
        )
        ips = [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        pass
    if not ips:
        # 回退：纯 socket 枚举（可能含虚拟网卡）
        try:
            for info in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM
            ):
                addr = info[4][0]
                if addr not in ips and addr.startswith(("10.", "192.")):
                    ips.append(addr)
        except OSError:
            pass
    primary = _default_route_ip()
    if primary in ips:
        ips.remove(primary)
        ips.insert(0, primary)
    return ips


def list_adapters(up_only=True):
    """列出网卡名称；up_only 失败时回退到全部网卡（与 bat 行为一致）。"""
    cond = " | Where-Object {$_.Status -eq 'Up'}" if up_only else ""
    out = _run_powershell(f"Get-NetAdapter{cond} | Select-Object -ExpandProperty Name")
    names = [line.strip() for line in out.splitlines() if line.strip()]
    return names


def get_adapter_config(nic):
    """返回 netsh 当前配置文本。"""
    proc = subprocess.run(
        ["netsh", "interface", "ip", "show", "config", f"name={nic}"],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        return "[错误] 无法获取配置信息，请检查网卡名称"
    return proc.stdout.strip()


def valid_ip(ip):
    try:
        socket.inet_aton(ip)
        return ip.count(".") == 3 and ip != "0.0.0.0"
    except OSError:
        return False


# ---------------------------------------------------------------- 提权执行

def _elevated_run(script_body):
    """写临时 ps1 → UAC 提权执行 → 读结果文件。返回 (ok, msg)。"""
    tmpdir = tempfile.gettempdir()
    script_path = os.path.join(tmpdir, "ipswitch_apply.ps1")
    result_path = os.path.join(tmpdir, "ipswitch_result.txt")
    if os.path.exists(result_path):
        os.remove(result_path)
    with open(script_path, "w", encoding="utf-8-sig") as f:
        f.write(script_body.replace("@RESULT@", result_path))

    arglist = "'-NoProfile','-ExecutionPolicy','Bypass','-File','{}'".format(
        script_path.replace("'", "''")
    )
    cmd = f"Start-Process powershell -Verb RunAs -Wait -ArgumentList {arglist}"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=CREATE_NO_WINDOW,
    )

    if not os.path.exists(result_path):
        return False, "提权被取消或执行失败"
    with open(result_path, encoding="utf-8", errors="replace") as f:
        codes = [line.strip() for line in f if line.strip()]
    bad = [c for c in codes if not c.endswith(":0")]
    if bad:
        return False, "命令执行失败: " + ", ".join(codes)
    return True, "配置已应用"


def apply_dhcp(nic):
    body = "\n".join(
        [
            '$ErrorActionPreference = "Continue"',
            f'netsh interface ip set address name="{nic}" source=dhcp',
            'Add-Content -Path "@RESULT@" -Value ("addr:" + $LASTEXITCODE)',
            f'netsh interface ip set dns name="{nic}" source=dhcp',
            'Add-Content -Path "@RESULT@" -Value ("dns:" + $LASTEXITCODE)',
        ]
    )
    return _elevated_run(body)


def apply_static(nic, ip, mask, gateway, dns1, dns2=""):
    body = [
        '$ErrorActionPreference = "Continue"',
        f'netsh interface ip set address name="{nic}" source=static '
        f"address={ip} mask={mask} gateway={gateway}",
        'Add-Content -Path "@RESULT@" -Value ("addr:" + $LASTEXITCODE)',
        f'netsh interface ip set dns name="{nic}" source=static address={dns1} validate=no',
        'Add-Content -Path "@RESULT@" -Value ("dns1:" + $LASTEXITCODE)',
    ]
    if dns2:
        body.append(
            f'netsh interface ip add dns name="{nic}" address={dns2} index=2 validate=no'
        )
        body.append('Add-Content -Path "@RESULT@" -Value ("dns2:" + $LASTEXITCODE)')
    return _elevated_run("\n".join(body))


# ---------------------------------------------------------------- 切换对话框

class IPSwitchDialog(tk.Toplevel):
    """IP 配置切换对话框：网卡选择 / 预设 / 自定义 / DHCP / 查看配置。"""

    def __init__(self, master, on_applied=None):
        super().__init__(master)
        self.on_applied = on_applied
        self.title("IP 配置切换")
        self.configure(bg="white")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self._busy = False
        self.presets = load_presets()

        self.nic_var = tk.StringVar()
        self.last_var = tk.StringVar(value="100")
        self.ip_var = tk.StringVar()
        self.mask_var = tk.StringVar(value="255.255.255.0")
        self.gw_var = tk.StringVar()
        self.dns1_var = tk.StringVar()
        self.dns2_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")

        self._build_ui()
        self._load_adapters()

        self.update_idletasks()
        x = master.winfo_rootx()
        y = max(10, master.winfo_rooty() - self.winfo_reqheight() - 12)
        self.geometry(f"+{x}+{y}")
        self.focus_force()

    # ---- UI ----

    def _build_ui(self):
        pad = {"padx": 10, "pady": 3}
        head = tk.Label(
            self, text="IP 配置切换", fg="#4F46E5", bg="white",
            font=("微软雅黑", 13, "bold"),
        )
        head.pack(anchor="w", **pad)

        row = tk.Frame(self, bg="white")
        row.pack(fill="x", **pad)
        tk.Label(row, text="网卡:", bg="white", font=("微软雅黑", 10)).pack(side="left")
        self.nic_combo = ttk.Combobox(
            row, textvariable=self.nic_var, state="readonly", width=28
        )
        self.nic_combo.pack(side="left", padx=6)
        tk.Button(
            row, text="刷新", command=self._load_adapters,
            relief="flat", bg="#E5E7EB", cursor="hand2",
        ).pack(side="left")

        row2 = tk.Frame(self, bg="white")
        row2.pack(fill="x", **pad)
        self.btn_dhcp = tk.Button(
            row2, text="自动获取 (DHCP)", command=self._on_dhcp,
            relief="flat", bg="#10B981", fg="white", cursor="hand2", padx=10,
        )
        self.btn_dhcp.pack(side="left")
        tk.Button(
            row2, text="查看当前配置", command=self._on_show_config,
            relief="flat", bg="#E5E7EB", cursor="hand2", padx=10,
        ).pack(side="left", padx=8)

        tk.Label(
            self, text="预设方案（自动填入下方参数）:", bg="white",
            font=("微软雅黑", 10),
        ).pack(anchor="w", padx=10)
        row3 = tk.Frame(self, bg="white")
        row3.pack(fill="x", padx=10, pady=2)
        for preset in self.presets:
            tk.Button(
                row3, text=preset["name"], relief="flat", bg="#EEF2FF",
                cursor="hand2", padx=6,
                command=lambda p=preset: self._fill_preset(p),
            ).pack(side="left", padx=2)

        row4 = tk.Frame(self, bg="white")
        row4.pack(fill="x", **pad)
        tk.Label(row4, text="IP 末位:", bg="white", font=("微软雅黑", 10)).pack(side="left")
        tk.Entry(row4, textvariable=self.last_var, width=6).pack(side="left", padx=4)
        tk.Label(
            row4, text="(1-254，配合预设使用)", bg="white", fg="#888",
            font=("微软雅黑", 9),
        ).pack(side="left")

        grid = tk.Frame(self, bg="white")
        grid.pack(fill="x", padx=10, pady=4)
        fields = [
            ("IP 地址", self.ip_var), ("子网掩码", self.mask_var),
            ("默认网关", self.gw_var), ("首选 DNS", self.dns1_var),
            ("备用 DNS", self.dns2_var),
        ]
        for i, (label, var) in enumerate(fields):
            tk.Label(grid, text=label + ":", bg="white", font=("微软雅黑", 10)).grid(
                row=i, column=0, sticky="e", pady=2
            )
            tk.Entry(grid, textvariable=var, width=22).grid(
                row=i, column=1, sticky="w", padx=6, pady=2
            )

        row5 = tk.Frame(self, bg="white")
        row5.pack(fill="x", **pad)
        self.btn_apply = tk.Button(
            row5, text="应用静态配置", command=self._on_apply_static,
            relief="flat", bg="#4F46E5", fg="white", cursor="hand2", padx=14,
        )
        self.btn_apply.pack(side="left")
        tk.Button(
            row5, text="关闭", command=self.destroy,
            relief="flat", bg="#E5E7EB", cursor="hand2", padx=10,
        ).pack(side="right")

        tk.Label(
            self, textvariable=self.status_var, bg="white", fg="#666",
            font=("微软雅黑", 9), anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 8))

        self.config_text = tk.Text(
            self, height=7, width=52, font=("Consolas", 9),
            state="disabled", bg="#F9FAFB",
        )

    # ---- 数据 ----

    def _load_adapters(self):
        names = list_adapters(up_only=True) or list_adapters(up_only=False)
        self.nic_combo["values"] = names
        if names and not self.nic_var.get():
            self.nic_var.set(names[0])
        self.status_var.set(f"已加载 {len(names)} 块网卡")

    def _fill_preset(self, preset):
        last = self.last_var.get().strip()
        if not last.isdigit() or not 1 <= int(last) <= 254:
            last = "100"
            self.last_var.set(last)
        self.ip_var.set(f"{preset['base']}.{last}")
        self.mask_var.set(preset["mask"])
        self.gw_var.set(preset["gateway"])
        self.dns1_var.set(preset["dns1"])
        self.dns2_var.set(preset["dns2"])
        self.status_var.set(f"已填入预设: {preset['name']}")

    def _on_show_config(self):
        nic = self.nic_var.get()
        if not nic:
            messagebox.showwarning("提示", "请先选择网卡", parent=self)
            return
        cfg = get_adapter_config(nic)
        self.config_text.configure(state="normal")
        self.config_text.delete("1.0", "end")
        self.config_text.insert("1.0", cfg)
        self.config_text.configure(state="disabled")
        if not self.config_text.winfo_ismapped():
            self.config_text.pack(fill="x", padx=10, pady=(0, 6))
        self.status_var.set(f"当前配置: {nic}")

    # ---- 应用 ----

    def _run_async(self, func, confirm_msg):
        if self._busy:
            return
        nic = self.nic_var.get()
        if not nic:
            messagebox.showwarning("提示", "请先选择网卡", parent=self)
            return
        if not messagebox.askyesno("确认", confirm_msg, parent=self):
            return

        self._busy = True
        self._set_buttons(False)
        self.status_var.set("正在应用配置（请在 UAC 弹窗中确认）...")

        def worker():
            ok, msg = func(nic)
            self.after(0, lambda: self._apply_done(ok, msg))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_done(self, ok, msg):
        self._busy = False
        self._set_buttons(True)
        self.status_var.set(("✔ " if ok else "✘ ") + msg)
        if ok and self.on_applied is not None:
            self.on_applied()

    def _set_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        for btn in (self.btn_dhcp, self.btn_apply):
            btn.configure(state=state)

    def _on_dhcp(self):
        nic = self.nic_var.get()
        self._run_async(
            lambda n: apply_dhcp(n),
            f"确认将网卡\n\n  {nic}\n\n切换为自动获取 IP 和 DNS (DHCP)？",
        )

    def _on_apply_static(self):
        ip = self.ip_var.get().strip()
        mask = self.mask_var.get().strip()
        gw = self.gw_var.get().strip()
        dns1 = self.dns1_var.get().strip()
        dns2 = self.dns2_var.get().strip()

        if not all(valid_ip(x) for x in (ip, mask, gw, dns1 if dns1 else "1.1.1.1")):
            messagebox.showerror("错误", "IP / 掩码 / 网关 / DNS 格式不正确", parent=self)
            return
        if not mask:
            messagebox.showerror("错误", "子网掩码不能为空", parent=self)
            return

        nic = self.nic_var.get()
        cfg = (
            f"网卡:   {nic}\nIP:     {ip}\n掩码:   {mask}\n网关:   {gw}\n"
            f"DNS1:   {dns1}\n" + (f"DNS2:   {dns2}\n" if dns2 else "")
        )
        self._run_async(
            lambda n: apply_static(n, ip, mask, gw, dns1, dns2),
            f"确认应用以下配置？\n\n{cfg}",
        )
