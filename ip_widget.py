import tkinter as tk
from tkinter import Menu
import datetime
import time
import threading
import json
import os
import sys
import urllib.request

try:
    import winreg
except ImportError:
    winreg = None

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    pystray = None

try:
    import ip_switcher
except ImportError:
    ip_switcher = None

TRANSPARENT = "#010101"
APP_TITLE = "IP悬浮窗"
APP_VERSION = "1.2.0"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_REG_NAME = "IPShowTips"


class IPFloatMonitor:
    def __init__(self, root):
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.configure(bg=TRANSPARENT)
        try:
            root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass

        self.accent_color = "#4F46E5"
        self.dot_fill = "#334155"
        self.dot_outline = "#818CF8"
        self.dot_size = 36
        self.win_width = 300
        self.win_height = 120
        self.hide_delay = 120
        self.alpha_dot = 0.92
        self.alpha_win = 1.0
        self.anim_duration = 160
        self.anim_interval = 12

        self.timer_hide = None
        self.timer_anim = None
        self.margin_x = 16
        self.margin_bottom_ratio = 0.30
        self.anchor_right = True
        self.pos_x = 0
        self.pos_y = 0
        self.dot_anchor_x = 0
        self.dot_anchor_y = 0
        self.expand_anchor_x = "left"
        self.expand_anchor_y = "top"
        self.anim_start = 0.0
        self.anim_mode = None

        self.state = "dot"
        self.run_flag = True
        self.outside_since = None
        self.press_x_root = None
        self.press_y_root = None
        self.win_x_at_press = 0
        self.win_y_at_press = 0
        self.did_drag = False
        self.drag_threshold = 4
        self._pending_ip_ok = None
        self._pending_ip_error = False
        self._tick_timer = None
        self.tray_icon = None
        self.in_tray = False
        self.last_known_ip = None
        self.alert_window = None

        self.canvas_dot = tk.Canvas(
            root,
            width=self.dot_size,
            height=self.dot_size,
            bg=TRANSPARENT,
            highlightthickness=0,
            bd=0,
        )
        self.circle_id = None
        self.text_id = None
        self._draw_circle(self.dot_size)

        self.panel_main = tk.Frame(root, bg="white", padx=10, pady=6)

        self.label_ip = tk.Label(
            self.panel_main,
            text="[ 加载中 ]",
            fg=self.accent_color,
            bg="white",
            font=("Consolas", 16, "bold"),
        )
        self.label_ip.pack(pady=2)

        self.label_loc = tk.Label(
            self.panel_main,
            text="归属地: --",
            fg="#111",
            bg="white",
            font=("微软雅黑", 12),
        )
        self.label_loc.pack(pady=1)

        self.label_local = tk.Label(
            self.panel_main,
            text="内网: --",
            fg="#0F766E",
            bg="white",
            font=("Consolas", 12, "bold"),
        )
        self.label_local.pack(pady=1)

        self.label_refresh = tk.Label(
            self.panel_main,
            text="最后刷新: --:--:--",
            fg="#666",
            bg="white",
            font=("微软雅黑", 11),
        )
        self.label_refresh.pack(pady=1)

        self.label_version = tk.Label(
            self.panel_main,
            text=f"v{APP_VERSION}",
            fg="#BBB",
            bg="white",
            font=("微软雅黑", 9),
        )
        self.label_version.pack(pady=(2, 0))

        self._bind_leave(self.panel_main)
        for child in self.panel_main.winfo_children():
            self._bind_leave(child)

        self.canvas_dot.bind("<Button-1>", self.drag_start)
        self.canvas_dot.tag_bind("hover", "<Button-1>", self.drag_start)
        self.panel_main.bind("<Button-1>", self.drag_start)
        for child in self.panel_main.winfo_children():
            child.bind("<Button-1>", self.drag_start)

        self.auto_start_var = tk.BooleanVar(value=self.is_auto_start_enabled())
        self.menu_right = Menu(root, tearoff=0)
        self.menu_right.add_command(label="最小化到托盘", command=self.hide_to_tray)
        if ip_switcher is not None:
            self.menu_right.add_command(label="切换IP...", command=self.open_ip_switcher)
        self.menu_right.add_checkbutton(
            label="开机自启动",
            variable=self.auto_start_var,
            command=self._on_auto_start_menu,
        )
        self.menu_right.add_separator()
        self.menu_right.add_command(label="退出程序", command=self.close_app)
        root.bind("<Button-3>", self.show_menu)

        self._show_dot()
        self._measure_panel_size()
        self._apply_geometry(self.dot_size, self.dot_size)
        root.attributes("-alpha", self.alpha_dot)
        root.update_idletasks()

        self._setup_tray()
        self._start_clock()
        self.refresh_ip()
        self._hover_loop()

    def _bind_leave(self, widget):
        widget.bind("<Enter>", self.on_panel_enter)
        widget.bind("<Leave>", self.on_panel_leave)

    def _hover_loop(self):
        if not self.run_flag:
            return

        if self.state == "window":
            if self._pointer_inside():
                self.outside_since = None
            elif self.outside_since is None:
                self.outside_since = time.perf_counter()
            elif (time.perf_counter() - self.outside_since) * 1000 >= self.hide_delay:
                self.outside_since = None
                self.start_shrink()

        self.root.after(50, self._hover_loop)

    def _draw_circle(self, size):
        if self.circle_id is not None:
            self.canvas_dot.delete(self.circle_id)
        if self.text_id is not None:
            self.canvas_dot.delete(self.text_id)

        pad = max(2, size // 14)
        self.circle_id = self.canvas_dot.create_oval(
            pad,
            pad,
            size - pad,
            size - pad,
            fill=self.dot_fill,
            outline=self.dot_outline,
            width=2,
            tags="hover",
        )
        font_size = max(7, size // 5)
        self.text_id = self.canvas_dot.create_text(
            size // 2,
            size // 2,
            text="IP",
            fill="#E2E8F0",
            font=("微软雅黑", font_size, "bold"),
            tags="hover",
        )

    def _resize_circle(self, circle_size):
        pad = max(2, circle_size // 14)
        self.canvas_dot.config(width=circle_size, height=circle_size)
        self.canvas_dot.coords(
            self.circle_id,
            pad,
            pad,
            circle_size - pad,
            circle_size - pad,
        )
        font_size = max(7, circle_size // 5)
        self.canvas_dot.coords(self.text_id, circle_size // 2, circle_size // 2)
        self.canvas_dot.itemconfig(
            self.text_id,
            font=("微软雅黑", font_size, "bold"),
        )

    def _window_size(self):
        self.root.update_idletasks()
        ww = self.root.winfo_width()
        wh = self.root.winfo_height()
        if ww <= 1 or wh <= 1:
            if self.state in ("dot", "shrinking"):
                return self.dot_size, self.dot_size
            if self.state in ("window", "expanding"):
                return self.win_width, self.win_height
            return self.dot_size, self.dot_size
        return ww, wh

    def _pointer_inside(self):
        x, y = self.root.winfo_pointerxy()
        wx = self.root.winfo_rootx()
        wy = self.root.winfo_rooty()
        ww, wh = self._window_size()
        return wx <= x < wx + ww and wy <= y < wy + wh

    def _measure_panel_size(self):
        sample_ip = "123.123.123.123"
        sample_loc = "河北省沧州市 联通"
        sample_local = "内网: 192.168.1.100 / 10.0.0.100"
        self.label_ip.config(text=sample_ip)
        self.label_loc.config(text=sample_loc)
        self.label_local.config(text=sample_local)
        self._show_panel()
        self.root.update_idletasks()
        self.win_width = self.panel_main.winfo_reqwidth()
        self.win_height = self.panel_main.winfo_reqheight()
        self._show_dot()

    def _clamp_position(self, x, y, width, height):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        m = self.margin_x
        width = int(width)
        height = int(height)
        x = max(m, min(int(x), sw - width - m))
        y = max(m, min(int(y), sh - height - m))
        return x, y

    def _apply_geometry(self, width, height, for_anim=False):
        width = int(width)
        height = int(height)

        if for_anim:
            x, y = self._geometry_for_anim_size(width, height)
        elif self.anchor_right and self.state == "dot":
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = sw - width - self.margin_x
            y = int(sh * (1 - self.margin_bottom_ratio) - height)
            x, y = self._clamp_position(x, y, width, height)
        else:
            x, y = self._clamp_position(self.pos_x, self.pos_y, width, height)

        self.pos_x = x
        self.pos_y = y
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _resolve_expand_anchors(self):
        dot_x = self.dot_anchor_x
        dot_y = self.dot_anchor_y
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        m = self.margin_x

        grow_right = self.win_width - self.dot_size
        grow_down = self.win_height - self.dot_size
        space_right = sw - (dot_x + self.dot_size) - m
        space_left = dot_x - m
        space_bottom = sh - (dot_y + self.dot_size) - m
        space_top = dot_y - m

        if space_right < grow_right and space_left > space_right:
            self.expand_anchor_x = "right"
        else:
            self.expand_anchor_x = "left"

        if space_bottom < grow_down and space_top > space_bottom:
            self.expand_anchor_y = "bottom"
        else:
            self.expand_anchor_y = "top"

    def _geometry_for_anim_size(self, width, height):
        dot_x = self.dot_anchor_x
        dot_y = self.dot_anchor_y

        if self.expand_anchor_x == "right":
            x = dot_x + self.dot_size - width
        else:
            x = dot_x

        if self.expand_anchor_y == "bottom":
            y = dot_y + self.dot_size - height
        else:
            y = dot_y

        return self._clamp_position(x, y, width, height)

    def _sync_dot_anchor_from_window(self):
        curr_x = self.root.winfo_x()
        curr_y = self.root.winfo_y()
        curr_w, curr_h = self.win_width, self.win_height

        if self.expand_anchor_x == "right":
            self.dot_anchor_x = curr_x + curr_w - self.dot_size
        else:
            self.dot_anchor_x = curr_x

        if self.expand_anchor_y == "bottom":
            self.dot_anchor_y = curr_y + curr_h - self.dot_size
        else:
            self.dot_anchor_y = curr_y

    @staticmethod
    def _ease(t):
        return t * t * (3.0 - 2.0 * t)

    def _show_dot(self):
        self.panel_main.pack_forget()
        self.canvas_dot.pack()
        self.root.configure(bg=TRANSPARENT)

    def _show_panel(self):
        self.canvas_dot.pack_forget()
        self.panel_main.pack(fill="both", expand=True)
        self.root.configure(bg="white")

    def clear_anim_timer(self):
        if self.timer_hide:
            self.root.after_cancel(self.timer_hide)
            self.timer_hide = None
        if self.timer_anim:
            self.root.after_cancel(self.timer_anim)
            self.timer_anim = None

    def clear_all_timer(self):
        self.clear_anim_timer()
        if self._tick_timer:
            self.root.after_cancel(self._tick_timer)
            self._tick_timer = None

    def _now_second(self):
        return datetime.datetime.now().replace(microsecond=0)

    def _start_clock(self):
        self._on_second_tick()

    def _on_second_tick(self):
        if not self.run_flag:
            return

        now = self._now_second()
        self._flush_pending_ip(now)

        delay = 1000 - (datetime.datetime.now().microsecond // 1000)
        if delay <= 0:
            delay = 1000
        self._tick_timer = self.root.after(delay, self._on_second_tick)

    def _queue_ip_ok(self, ip, location):
        self._pending_ip_error = False
        self._pending_ip_ok = (ip, location)
        self.root.after(0, self._sync_ip_with_clock)

    def _queue_ip_error(self):
        self._pending_ip_ok = None
        self._pending_ip_error = True
        self.root.after(0, self._sync_ip_with_clock)

    def _sync_ip_with_clock(self):
        if not self.run_flag:
            return
        if not self._pending_ip_ok and not self._pending_ip_error:
            return
        self._flush_pending_ip(self._now_second())

    def _flush_pending_ip(self, now):
        if self._pending_ip_ok:
            ip, location = self._pending_ip_ok
            self._pending_ip_ok = None
            old_ip = self.last_known_ip
            if old_ip is not None and old_ip != ip:
                self._show_ip_change_alert(old_ip, ip, location)
            self.last_known_ip = ip
            self.label_ip.config(text=f"[ {ip} ]")
            self.label_loc.config(text=f"归属地: {location}")
            self.label_refresh.config(text=f"最后刷新: {now.strftime('%H:%M:%S')}")
            return

        if self._pending_ip_error:
            self._pending_ip_error = False
            self.label_ip.config(text="[ 网络异常 ]")
            self.label_loc.config(text="归属地: 获取失败")
            self.label_refresh.config(text=f"最后刷新: {now.strftime('%H:%M:%S')}")

    def _close_ip_alert(self):
        if self.alert_window is not None:
            try:
                if self.alert_window.winfo_exists():
                    self.alert_window.destroy()
            except tk.TclError:
                pass
            self.alert_window = None

    def _alert_position(self, width, height):
        dot_x = self.root.winfo_x()
        dot_y = self.root.winfo_y()
        dot_w, dot_h = self._window_size()
        x = dot_x + dot_w - width
        y = dot_y - height - 12
        if y < self.margin_x:
            y = dot_y + dot_h + 12
        return self._clamp_position(x, y, width, height)

    def _show_ip_change_alert(self, old_ip, new_ip, location):
        self._close_ip_alert()

        alert = tk.Toplevel(self.root)
        alert.overrideredirect(True)
        alert.configure(bg="white")
        alert.attributes("-topmost", True)

        shell = tk.Frame(
            alert,
            bg="white",
            highlightbackground="#D1D5DB",
            highlightthickness=1,
        )
        shell.pack(fill="both", expand=True)

        header = tk.Frame(shell, bg="white", padx=16)
        header.pack(fill="x", pady=(10, 0))

        tk.Label(
            header,
            text="检测到 IP 地址变化",
            fg=self.accent_color,
            bg="white",
            font=("微软雅黑", 13, "bold"),
        ).pack(anchor="w")

        frame = tk.Frame(shell, bg="white", padx=16, pady=10)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text=f"原 IP：{old_ip}",
            fg="#666",
            bg="white",
            font=("Consolas", 11),
        ).pack(anchor="w", pady=1)

        tk.Label(
            frame,
            text=f"新 IP：{new_ip}",
            fg="#111",
            bg="white",
            font=("Consolas", 12, "bold"),
        ).pack(anchor="w", pady=1)

        tk.Label(
            frame,
            text=f"归属地：{location}",
            fg="#444",
            bg="white",
            font=("微软雅黑", 11),
        ).pack(anchor="w", pady=(4, 10))

        btn_row = tk.Frame(frame, bg="white")
        btn_row.pack(anchor="e")
        tk.Button(
            btn_row,
            text="知道了",
            command=self._close_ip_alert,
            bg=self.accent_color,
            fg="white",
            activebackground="#4338CA",
            activeforeground="white",
            relief="flat",
            padx=14,
            pady=4,
            cursor="hand2",
        ).pack()

        alert.update_idletasks()
        width = max(300, shell.winfo_reqwidth() + 2)
        height = shell.winfo_reqheight() + 2
        x, y = self._alert_position(width, height)
        alert.geometry(f"{width}x{height}+{x}+{y}")

        self.alert_window = alert
        alert.lift()

    def start_expand(self):
        if self.state in ("window", "expanding"):
            return
        self.clear_anim_timer()
        self.dot_anchor_x = self.root.winfo_x()
        self.dot_anchor_y = self.root.winfo_y()
        self._resolve_expand_anchors()
        self._show_dot()
        self.anim_mode = "expand"
        self.anim_start = time.perf_counter()
        self.state = "expanding"
        self._tick_anim()

    def start_shrink(self):
        if self.state in ("dot", "shrinking"):
            return
        self.clear_anim_timer()
        self._sync_dot_anchor_from_window()
        self._show_panel()
        self.anim_mode = "shrink"
        self.anim_start = time.perf_counter()
        self.state = "shrinking"
        self._tick_anim()

    def _tick_anim(self):
        elapsed = (time.perf_counter() - self.anim_start) * 1000
        t = min(elapsed / self.anim_duration, 1.0)
        eased = self._ease(t)

        if self.anim_mode == "expand":
            w = self.dot_size + (self.win_width - self.dot_size) * eased
            h = self.dot_size + (self.win_height - self.dot_size) * eased
            alpha = self.alpha_dot + (self.alpha_win - self.alpha_dot) * eased

            self._apply_geometry(w, h, for_anim=True)
            self.root.attributes("-alpha", alpha)

            if eased < 0.55:
                self._show_dot()
                self._resize_circle(int(min(w, h)))
            else:
                self._show_panel()
                panel_alpha = min(1.0, (eased - 0.55) / 0.45)
                self.root.attributes("-alpha", self.alpha_dot + (self.alpha_win - self.alpha_dot) * panel_alpha)

            if t >= 1.0:
                self._apply_geometry(self.win_width, self.win_height, for_anim=True)
                self.root.attributes("-alpha", self.alpha_win)
                self._show_panel()
                self.state = "window"
                self.anim_mode = None
                self.timer_anim = None
                return

        elif self.anim_mode == "shrink":
            w = self.win_width - (self.win_width - self.dot_size) * eased
            h = self.win_height - (self.win_height - self.dot_size) * eased
            alpha = self.alpha_win - (self.alpha_win - self.alpha_dot) * eased

            self._apply_geometry(w, h, for_anim=True)
            self.root.attributes("-alpha", alpha)

            if eased < 0.45:
                self._show_panel()
            else:
                self._show_dot()
                self._resize_circle(max(self.dot_size, int(min(w, h))))

            if t >= 1.0:
                self._resize_circle(self.dot_size)
                self._apply_geometry(self.dot_size, self.dot_size, for_anim=True)
                self.root.attributes("-alpha", self.alpha_dot)
                self._show_dot()
                self.state = "dot"
                self.anim_mode = None
                self.timer_anim = None
                return

        self.timer_anim = self.root.after(self.anim_interval, self._tick_anim)

    def on_panel_enter(self, _event=None):
        self.outside_since = None

    def on_panel_leave(self, _event=None):
        if self.state != "window":
            return
        self.outside_since = time.perf_counter()

    def drag_start(self, event):
        if self.press_x_root is not None:
            return "break"
        self.press_x_root = event.x_root
        self.press_y_root = event.y_root
        self.win_x_at_press = self.root.winfo_x()
        self.win_y_at_press = self.root.winfo_y()
        self.did_drag = False
        self.root.bind_all("<B1-Motion>", self.drag_move, add="+")
        self.root.bind_all("<ButtonRelease-1>", self.drag_end, add="+")
        return "break"

    def drag_move(self, event):
        if self.press_x_root is None:
            return
        dx = event.x_root - self.press_x_root
        dy = event.y_root - self.press_y_root
        if not self.did_drag:
            if abs(dx) + abs(dy) <= self.drag_threshold:
                return
            self.did_drag = True
        self.anchor_right = False
        ww, wh = self._window_size()
        self.pos_x, self.pos_y = self._clamp_position(
            self.win_x_at_press + dx,
            self.win_y_at_press + dy,
            ww,
            wh,
        )
        self.root.geometry(f"{ww}x{wh}+{self.pos_x}+{self.pos_y}")

    def _stop_drag_tracking(self):
        self.root.unbind_all("<B1-Motion>")
        self.root.unbind_all("<ButtonRelease-1>")

    def _was_dragged(self):
        if self.did_drag:
            return True
        dx = abs(self.root.winfo_x() - self.win_x_at_press)
        dy = abs(self.root.winfo_y() - self.win_y_at_press)
        return dx + dy > self.drag_threshold

    def drag_end(self, event):
        if self.press_x_root is None:
            return

        dragged = self._was_dragged()
        self._stop_drag_tracking()

        if dragged:
            ww, wh = self._window_size()
            self.pos_x, self.pos_y = self._clamp_position(
                self.root.winfo_x(),
                self.root.winfo_y(),
                ww,
                wh,
            )
            self.root.geometry(f"{ww}x{wh}+{self.pos_x}+{self.pos_y}")

        if self.state == "dot" and not dragged:
            self.start_expand()

        self.press_x_root = None
        self.press_y_root = None
        self.did_drag = False

    def _create_tray_image(self):
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((6, 6, size - 6, size - 6), fill="#334155", outline="#818CF8", width=3)
        try:
            font = ImageFont.truetype("msyh.ttc", 18)
        except OSError:
            font = ImageFont.load_default()
        draw.text((size // 2, size // 2), "IP", fill="#E2E8F0", font=font, anchor="mm")
        return image

    def _build_tray_menu(self):
        items = []
        if self.in_tray:
            items.append(
                pystray.MenuItem(
                    "显示悬浮球",
                    lambda: self.root.after(0, self._show_from_tray_main),
                    default=True,
                )
            )
        else:
            items.append(
                pystray.MenuItem(
                    "最小化到托盘",
                    lambda: self.root.after(0, self._hide_to_tray_main),
                    default=True,
                )
            )
        items.extend(
            [
                pystray.MenuItem(
                    "开机自启动",
                    lambda: self.root.after(0, self.toggle_auto_start),
                    checked=lambda _item: self.is_auto_start_enabled(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda: self.root.after(0, self.close_app)),
            ]
        )
        return pystray.Menu(*items)

    def _setup_tray(self):
        if pystray is None:
            return

        self.tray_icon = pystray.Icon(
            APP_TITLE,
            self._create_tray_image(),
            f"{APP_TITLE} v{APP_VERSION}",
            self._build_tray_menu(),
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _refresh_tray_menu(self):
        if self.tray_icon is not None:
            self.tray_icon.menu = self._build_tray_menu()

    @staticmethod
    def _get_launch_command():
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        script = os.path.abspath(__file__)
        return f'"{sys.executable}" "{script}"'

    def is_auto_start_enabled(self):
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH) as key:
                value, _ = winreg.QueryValueEx(key, RUN_REG_NAME)
            return bool(value)
        except OSError:
            return False

    def enable_auto_start(self):
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(
                    key, RUN_REG_NAME, 0, winreg.REG_SZ, self._get_launch_command()
                )
            return True
        except OSError:
            return False

    def disable_auto_start(self):
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, RUN_REG_NAME)
            return True
        except OSError:
            return False

    def toggle_auto_start(self):
        if self.is_auto_start_enabled():
            self.disable_auto_start()
        else:
            self.enable_auto_start()
        self.auto_start_var.set(self.is_auto_start_enabled())
        self._refresh_tray_menu()

    def _on_auto_start_menu(self):
        if self.auto_start_var.get():
            ok = self.enable_auto_start()
        else:
            ok = self.disable_auto_start()
        self.auto_start_var.set(self.is_auto_start_enabled() if ok else not self.auto_start_var.get())
        self._refresh_tray_menu()

    def hide_to_tray(self):
        self.root.after(0, self._hide_to_tray_main)

    def _hide_to_tray_main(self):
        if not self.run_flag:
            return
        if self.state != "dot" and self.state not in ("shrinking",):
            self.start_shrink()
        self.root.after(200, self._withdraw_window)

    def _withdraw_window(self):
        if self.run_flag:
            self.root.withdraw()
            self.in_tray = True
            self._refresh_tray_menu()

    def _show_from_tray_main(self):
        if not self.run_flag:
            return
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.in_tray = False
        self._refresh_tray_menu()

    def show_menu(self, event):
        self.menu_right.post(event.x_root, event.y_root)

    def close_app(self):
        if not self.run_flag:
            return
        self.run_flag = False
        self._stop_drag_tracking()
        self.clear_all_timer()
        self._close_ip_alert()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.root.destroy()

    def refresh_ip(self):
        if not self.run_flag:
            return
        self.root.after(10000, self.refresh_ip)
        threading.Thread(target=self._fetch_ip_background, daemon=True).start()

    def _fetch_ip_background(self):
        self.update_local_ips()
        try:
            req = urllib.request.Request(
                "https://whois.pconline.com.cn/ipJson.jsp?json=true",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                },
            )
            with urllib.request.urlopen(req, timeout=3) as res:
                data = json.loads(res.read().decode("gbk"))
            ip = data["ip"]
            location = data.get("addr", f"{data.get('pro', '')}{data.get('city', '')}")
            self.root.after(0, self._queue_ip_ok, ip, location)
        except Exception:
            self.root.after(0, self._queue_ip_error)

    def update_local_ips(self):
        """获取本机内网 IP（10.* / 192.*）并更新标签，可从任意线程调用。"""
        if ip_switcher is None:
            return
        try:
            ips = ip_switcher.list_local_ips()
        except Exception:
            ips = []
        self.root.after(0, self._set_local_label, ips)

    def _set_local_label(self, ips):
        if not self.run_flag:
            return
        text = "内网: " + (" / ".join(ips) if ips else "--")
        self.label_local.config(text=text)

    def open_ip_switcher(self):
        if ip_switcher is None:
            return

        def on_applied():
            self.update_local_ips()

        ip_switcher.IPSwitchDialog(self.root, on_applied=on_applied)


if __name__ == "__main__":
    try:
        window = tk.Tk()
        app = IPFloatMonitor(window)
        window.mainloop()
    except KeyboardInterrupt:
        pass
