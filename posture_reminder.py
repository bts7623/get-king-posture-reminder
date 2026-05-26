#!/usr/bin/env python3
"""
자세 알리미 (Posture Reminder)
- 시스템 트레이 상주, 우클릭으로 설정/종료
- 주기적으로 만화풍 자세 교정 팝업 표시 (2프레임 애니메이션)
- 최초 실행 시 환영 화면 표시
- 중복 실행 방지 (기존 인스턴스 자동 종료)
- 부팅 시 자동 실행 옵션
"""

import tkinter as tk
import threading
import time
import sys
import os
import math
import random
import subprocess
import json
from datetime import datetime

try:
    import winreg
except ImportError:
    winreg = None


def _ensure_deps():
    missing = []
    try:
        import pystray  # noqa: F401
        from PIL import Image, ImageDraw  # noqa: F401
    except ImportError:
        missing += ["pystray", "Pillow"]
    try:
        from screeninfo import get_monitors  # noqa: F401
    except ImportError:
        missing.append("screeninfo")
    if missing:
        print("필요한 패키지를 설치 중... (최초 1회)")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install"] + missing + ["--quiet"],
        )


_ensure_deps()

import pystray
from PIL import Image, ImageDraw


# ── 상수 ──────────────────────────────────────────────────────────────────

VERSION = "1.0.1"

ALL_MONITORS = -1   # target_monitor_index 특수값: 모든 모니터에 동시 표시

DEFAULT_INTERVAL = 30
DEFAULT_DURATION = 10

# 장면별 2프레임 애니메이션: [(tag, dx, dy), ...]
ANIM_CONFIGS = [
    [("anim", 13, 0)],                          # 장면 1: 목/머리 앞뒤
    [("anim_body", 0, 7), ("anim_zzz", 0, -7)], # 장면 2: 머리 꾸벅 + ZZZ 둥실
    [("anim", 7, 0)],                           # 장면 3: 나쁜 자세 덜덜
    [("anim", 0, -10)],                         # 장면 4: 팔 위아래 펌프
    [("anim", 14, 0)],                          # 장면 5: 목+머리 좌우 틸트
    [("anim", 0, -11)],                         # 장면 6: 어깨 위아래 으쓱
]
ANIM_INTERVAL_MS = 550

PID_FILE = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "posture_reminder.pid")
APP_REG_NAME = "PostureReminder"
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PostureReminder")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")


# ── 중복 실행 방지 ─────────────────────────────────────────────────────────

def ensure_single_instance():
    """이미 실행 중인 인스턴스를 종료하고 현재 인스턴스 PID를 등록"""
    current_pid = os.getpid()

    if getattr(sys, "frozen", False):
        # exe 모드 ①: tasklist로 posture_reminder*.exe 이름 검색 (exe→exe 감지)
        killed = False
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = [p.strip('"') for p in line.strip().split('","')]
                if len(parts) >= 2 and "posture_reminder" in parts[0].lower():
                    pid = parts[1].strip('"')
                    if pid.isdigit() and int(pid) != current_pid:
                        subprocess.run(["taskkill", "/F", "/PID", pid],
                                      capture_output=True, timeout=3)
                        killed = True
        except Exception:
            pass

        # exe 모드 ②: PID 파일 확인 (script→exe 감지 — pythonw.exe는 이름 검색에 안 걸림)
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE) as f:
                    old_pid = f.read().strip()
                if old_pid.isdigit() and int(old_pid) != current_pid:
                    subprocess.run(["taskkill", "/F", "/PID", old_pid],
                                  capture_output=True, timeout=3)
                    killed = True
            except Exception:
                pass

        if killed:
            time.sleep(0.8)
    else:
        # 스크립트 모드: PID 파일로 이전 인스턴스 종료
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE) as f:
                    old_pid = f.read().strip()
                if old_pid.isdigit() and int(old_pid) != current_pid:
                    subprocess.run(["taskkill", "/F", "/PID", old_pid],
                                  capture_output=True, timeout=3)
                    time.sleep(0.8)
            except Exception:
                pass

    try:
        with open(PID_FILE, "w") as f:
            f.write(str(current_pid))
    except Exception:
        pass


# ── 메인 클래스 ───────────────────────────────────────────────────────────

class PostureReminder:
    def __init__(self):
        self.interval_minutes = DEFAULT_INTERVAL
        self.duration_seconds = DEFAULT_DURATION
        self.running = True
        self.last_shown = time.time()
        self.last_notified: float | None = None  # 실제 알림이 울린 시각
        self.icon = None
        self.scene_index = 0
        self.scene_queue: list[int] = []   # 셔플 큐 — 비면 다시 채움
        self.target_monitor_index = 0
        self._load_settings()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

        self.scenes = [
            self._draw_turtle_neck,
            self._draw_hunched_monkey,
            self._draw_shoulder_reminder,
            self._draw_banzai_stretch,
            self._draw_neck_tilt,
            self._draw_shoulder_shrug,
        ]

    # ── 설정 저장/불러오기 ────────────────────────────────────────────────

    def _load_settings(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.interval_minutes = int(data.get("interval_minutes", DEFAULT_INTERVAL))
            self.duration_seconds = int(data.get("duration_seconds", DEFAULT_DURATION))
            self.target_monitor_index = int(data.get("target_monitor_index", 0))
        except Exception:
            pass  # 파일 없거나 손상 시 기본값 유지

    def _save_settings(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "interval_minutes": self.interval_minutes,
                    "duration_seconds": self.duration_seconds,
                    "target_monitor_index": self.target_monitor_index,
                }, f, indent=2)
        except Exception:
            pass

    # ── 모니터 감지 ───────────────────────────────────────────────────────

    def _get_monitors(self):
        try:
            from screeninfo import get_monitors
            return get_monitors()
        except Exception:
            class _Mon:
                x = 0; y = 0
                width = self.root.winfo_screenwidth()
                height = self.root.winfo_screenheight()
                name = "Primary"; is_primary = True
            return [_Mon()]

    # ── 자동 실행 ─────────────────────────────────────────────────────────

    def _get_autostart_cmd(self):
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        script = os.path.abspath(__file__)
        exe = pythonw if os.path.exists(pythonw) else sys.executable
        return f'"{exe}" "{script}"'

    def _is_autostart(self):
        if winreg is None:
            return False
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, APP_REG_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False

    def _set_autostart(self, enable: bool):
        if winreg is None:
            return
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_WRITE)
            if enable:
                winreg.SetValueEx(key, APP_REG_NAME, 0, winreg.REG_SZ, self._get_autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(key, APP_REG_NAME)
                except Exception:
                    pass
            winreg.CloseKey(key)
        except Exception:
            pass

    # ── 진입점 ────────────────────────────────────────────────────────────

    def start(self):
        threading.Thread(target=self._run_tray, daemon=True).start()
        threading.Thread(target=self._timer_loop, daemon=True).start()
        self.root.after(700, self._show_welcome)
        self.root.mainloop()

    # ── 트레이 ────────────────────────────────────────────────────────────

    def _run_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("지금 알림 보기", self._trigger_now),
            pystray.MenuItem("환경 설정", self._open_settings),
            pystray.MenuItem("알림 갤러리", self._open_gallery),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", self._quit),
        )
        self.icon = pystray.Icon(
            "posture-reminder",
            self._make_tray_image(),
            f"자세 알리미 v{VERSION} (우클릭: 설정/종료)",
            menu,
        )
        self.icon.run()

    def _make_tray_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([2, 2, 62, 62], fill="#4ECDC4")
        d.ellipse([24, 8, 40, 24], fill="white")
        d.line([32, 24, 32, 50], fill="white", width=4)
        d.line([16, 34, 48, 34], fill="white", width=4)
        d.line([16, 34, 10, 50], fill="white", width=3)
        d.line([48, 34, 54, 50], fill="white", width=3)
        d.line([32, 50, 22, 64], fill="white", width=3)
        d.line([32, 50, 42, 64], fill="white", width=3)
        return img

    def _trigger_now(self, icon=None, item=None):
        self.root.after(0, self._show_reminder)

    def _open_settings(self, icon=None, item=None):
        self.root.after(0, self._show_settings_window)

    def _open_gallery(self, icon=None, item=None):
        self.root.after(0, self._show_gallery)

    def _quit(self, icon=None, item=None):
        self.running = False
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except Exception:
            pass
        if self.icon:
            self.icon.stop()
        self.root.after(0, self.root.quit)

    # ── 타이머 ────────────────────────────────────────────────────────────

    def _timer_loop(self):
        self.last_shown = time.time()
        while self.running:
            time.sleep(1)
            if not self.running:
                break
            if time.time() - self.last_shown >= self.interval_minutes * 60:
                self.last_shown = time.time()
                self.root.after(0, self._show_reminder)

    # ── 팝업 공통 로직 ────────────────────────────────────────────────────

    def _make_popup(self, w, h, monitor_index=None):
        """지정 모니터 중앙에 팝업 윈도우 생성"""
        idx = monitor_index if monitor_index is not None else self.target_monitor_index
        monitors = self._get_monitors()
        mon = monitors[min(idx, len(monitors) - 1)]

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.attributes("-alpha", 0.0)

        x = mon.x + (mon.width - w) // 2
        y = mon.y + (mon.height - h) // 2
        popup.geometry(f"{w}x{h}+{x}+{y}")

        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(popup.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), ctypes.sizeof(ctypes.c_int)
            )
        except Exception:
            pass
        return popup

    def _attach_dismiss(self, popup, canvas, duration, text_color="white", group=None):
        """클릭 또는 n초 후 닫기 + 카운트다운 + 페이드인"""
        remaining = [duration]
        cnt_id = canvas.create_text(
            canvas.winfo_reqwidth() // 2, canvas.winfo_reqheight() - 16,
            text=f"클릭하거나 {remaining[0]}초 후 자동으로 사라집니다",
            fill=text_color, font=("맑은 고딕", 9),
        )

        def dismiss(event=None):
            targets = group if group else [popup]
            for p in targets:
                try:
                    p.destroy()
                except Exception:
                    pass

        popup.bind("<Button-1>", dismiss)
        canvas.bind("<Button-1>", dismiss)
        popup.after(duration * 1000, dismiss)

        def tick():
            if not popup.winfo_exists():
                return
            remaining[0] -= 1
            if remaining[0] > 0:
                canvas.itemconfig(cnt_id, text=f"클릭하거나 {remaining[0]}초 후 자동으로 사라집니다")
                popup.after(1000, tick)
        popup.after(1000, tick)

        def fade(a=0.0):
            if not popup.winfo_exists():
                return
            a = min(a + 0.1, 0.95)
            popup.attributes("-alpha", a)
            if a < 0.95:
                popup.after(25, lambda: fade(a))
        fade()

    def _start_anim(self, popup, canvas, config):
        """2프레임 애니메이션 루프"""
        frame_n = [0]
        def tick():
            if not popup.winfo_exists():
                return
            sign = 1 if frame_n[0] % 2 == 0 else -1
            for tag, dx, dy in config:
                canvas.move(tag, dx * sign, dy * sign)
            frame_n[0] += 1
            popup.after(ANIM_INTERVAL_MS, tick)
        popup.after(ANIM_INTERVAL_MS, tick)

    # ── 환영 화면 ─────────────────────────────────────────────────────────

    def _show_welcome(self):
        W, H = 520, 420
        popup = self._make_popup(W, H)
        canvas = tk.Canvas(popup, width=W, height=H, highlightthickness=0, cursor="hand2")
        canvas.pack(fill="both", expand=True)

        self._draw_welcome(canvas, W, H)
        self._start_anim(popup, canvas, [("anim", 0, -9)])
        self._attach_dismiss(popup, canvas, self.duration_seconds, text_color="#065F46")

    def _draw_welcome(self, c, W, H):
        # 밝은 배경 (민트 화이트)
        c.create_rectangle(0, 0, W, H, fill="#ECFDF5", outline="")
        c.create_rectangle(0, H * 0.55, W, H, fill="#D1FAF5", outline="")

        # 장식 원 — 밝은 파스텔
        c.create_oval(-90, -90, 260, 260, fill="#A7F3D0", outline="")
        c.create_oval(W - 210, H - 210, W + 90, H + 90, fill="#FEF9C3", outline="")

        # 태양 (우상단)
        c.create_oval(W - 85, -85, W + 115, 115, fill="#FDE68A", outline="#FCD34D", width=3)
        for angle in range(0, 360, 40):
            r = math.radians(angle)
            bx = W + 15 + 78 * math.cos(r)
            by = 15 + 78 * math.sin(r)
            ex = W + 15 + 100 * math.cos(r)
            ey = 15 + 100 * math.sin(r)
            c.create_line(bx, by, ex, ey, fill="#FCD34D", width=3, capstyle="round")

        # 반짝이 점 (크고 선명한 무지개색)
        rng = random.Random(77)
        palette = ["#34D399", "#60A5FA", "#F472B6", "#FBBF24", "#A78BFA", "#FB923C", "#4ADE80"]
        for _ in range(26):
            sx = rng.randint(10, W - 10)
            sy = rng.randint(10, H - 90)
            r = rng.randint(3, 7)
            c.create_oval(sx - r, sy - r, sx + r, sy + r,
                         fill=rng.choice(palette), outline="white", width=1)

        # 제목 (어두운 텍스트 — 밝은 배경 위)
        c.create_text(W // 2 + 2, 46, text="바른 자세 알리미",
                     fill="#A7F3D0", font=("맑은 고딕", 24, "bold"))
        c.create_text(W // 2, 44, text="바른 자세 알리미",
                     fill="#065F46", font=("맑은 고딕", 24, "bold"))
        c.create_text(W // 2, 76, text="시작되었습니다! 🎉",
                     fill="#047857", font=("맑은 고딕", 15))

        # ── 환영 캐릭터 ───────────────────────────────────────────────────
        cx, cy = W // 2, 222

        # 캐릭터 뒤 광채 원
        c.create_oval(cx - 72, cy - 105, cx + 72, cy + 128,
                     fill="#CCFBF1", outline="#A7F3D0", width=2)

        # 다리/발 (정적)
        c.create_line(cx, cy + 10, cx, cy + 65, fill="#0284C7", width=7)
        c.create_line(cx, cy + 65, cx - 22, cy + 112, fill="#0284C7", width=6)
        c.create_line(cx, cy + 65, cx + 22, cy + 112, fill="#0284C7", width=6)
        c.create_oval(cx - 50, cy + 108, cx - 8, cy + 122, fill="#075985", outline="")   # 왼발 (왼쪽 방향)
        c.create_oval(cx + 8, cy + 108, cx + 50, cy + 122, fill="#075985", outline="")  # 오른발 (오른쪽 방향)

        # 머리 + 상체 (anim — 바운스)
        c.create_oval(cx - 28, cy - 80, cx + 28, cy - 24, fill="#FDBCB4",
                     outline="#E59866", width=2, tags="anim")
        c.create_arc(cx - 28, cy - 80, cx + 28, cy - 52,
                    start=0, extent=180, fill="#7C3AED", outline="", tags="anim")
        c.create_arc(cx - 15, cy - 66, cx - 3, cy - 54,
                    start=200, extent=140, outline="#374151", width=2, tags="anim")
        c.create_arc(cx + 3, cy - 66, cx + 15, cy - 54,
                    start=200, extent=140, outline="#374151", width=2, tags="anim")
        c.create_oval(cx - 22, cy - 52, cx - 12, cy - 44, fill="#FCA5A5", outline="", tags="anim")
        c.create_oval(cx + 12, cy - 52, cx + 22, cy - 44, fill="#FCA5A5", outline="", tags="anim")
        c.create_arc(cx - 13, cy - 46, cx + 13, cy - 32,
                    start=200, extent=140, outline="#DC2626", width=2, tags="anim")
        c.create_line(cx, cy - 24, cx, cy + 10, fill="#0284C7", width=7, tags="anim")
        c.create_line(cx - 30, cy - 8, cx + 30, cy - 8, fill="#0284C7", width=6, tags="anim")
        c.create_line(cx - 30, cy - 8, cx - 58, cy - 52, fill="#0284C7", width=5, tags="anim")
        c.create_line(cx + 30, cy - 8, cx + 58, cy - 52, fill="#0284C7", width=5, tags="anim")
        c.create_text(cx + 17, cy - 14, text="K&B", fill="#0369A1",
                      font=("Arial", 6, "bold"), tags="anim")
        c.create_oval(cx - 66, cy - 62, cx - 50, cy - 46, fill="#FDBCB4", outline="", tags="anim")
        c.create_oval(cx + 50, cy - 62, cx + 66, cy - 46, fill="#FDBCB4", outline="", tags="anim")
        c.create_text(cx - 82, cy - 74, text="✨", font=("Arial", 14), tags="anim")
        c.create_text(cx + 82, cy - 74, text="✨", font=("Arial", 14), tags="anim")

        # 하단 안내
        c.create_text(W // 2, H - 54,
                     text=f"{self.interval_minutes}분마다 자세를 알려드릴게요  💪",
                     fill="#047857", font=("맑은 고딕", 13, "bold"))
        c.create_text(W // 2, H - 30,
                     text="트레이 아이콘 우클릭 → 환경 설정으로 변경 가능합니다",
                     fill="#6B7280", font=("맑은 고딕", 9))

    # ── 자세 알림 팝업 ─────────────────────────────────────────────────────

    def _show_reminder(self, monitor_index=None, preview=False):
        if preview:
            scene_idx = self.scene_index % len(self.scenes)
        else:
            if not self.scene_queue:
                self.scene_queue = list(range(len(self.scenes)))
                random.shuffle(self.scene_queue)
            scene_idx = self.scene_queue.pop(0)
            self.last_notified = time.time()

        draw_fn = self.scenes[scene_idx]

        W, H = 520, 430
        target = monitor_index if monitor_index is not None else self.target_monitor_index
        indices = list(range(len(self._get_monitors()))) if target == ALL_MONITORS else [target]

        popups = []
        pairs = []
        for idx in indices:
            popup = self._make_popup(W, H, idx)
            canvas = tk.Canvas(popup, width=W, height=H, highlightthickness=0, cursor="hand2")
            canvas.pack(fill="both", expand=True)
            draw_fn(canvas, W, H)
            self._start_anim(popup, canvas, ANIM_CONFIGS[scene_idx])
            popups.append(popup)
            pairs.append((popup, canvas))

        group = popups if len(popups) > 1 else None
        for popup, canvas in pairs:
            self._attach_dismiss(popup, canvas, self.duration_seconds, group=group)

    # ── 장면 그리기 ────────────────────────────────────────────────────────

    def _draw_turtle_neck(self, c, W, H):
        """장면 1: 거북목  /  anim: 목+머리 앞뒤"""
        c.create_rectangle(0, 0, W, H, fill="#0B3D38", outline="")
        c.create_rectangle(0, H * 0.55, W, H, fill="#072C28", outline="")
        c.create_oval(-40, -40, 160, 160, fill="#155149", outline="")
        c.create_oval(W - 120, H - 120, W + 40, H + 40, fill="#155149", outline="")

        for dx, dy, col in [(2, 2, "#072C28"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx, 40 + dy, text="🐢  거북목 경보!",
                         fill=col, font=("맑은 고딕", 23, "bold"))

        dy = 300
        c.create_rectangle(70, dy, W - 70, dy + 16, fill="#7B5B0C", outline="#5A4008", width=2)
        c.create_rectangle(140, dy + 16, 178, dy + 60, fill="#7B5B0C", outline="#5A4008", width=2)
        c.create_rectangle(330, dy + 16, 368, dy + 60, fill="#7B5B0C", outline="#5A4008", width=2)

        c.create_rectangle(300, 218, 435, 299, fill="#2C3E50", outline="#1A252F", width=3)
        c.create_rectangle(310, 228, 425, 291, fill="#16A085", outline="")
        for i in range(5):
            c.create_rectangle(318, 235 + i * 10, 418, 240 + i * 10, fill="#0E8C74", outline="")
        c.create_text(356, 232, text="K&B.exe", fill="#0D7A6A", font=("Consolas", 6))
        c.create_rectangle(353, 298, 382, 310, fill="#2C3E50", outline="")
        c.create_rectangle(338, 308, 397, 316, fill="#2C3E50", outline="")
        c.create_rectangle(285, 298, 440, 312, fill="#BDC3C7", outline="#95A5A6", width=1)
        for col in range(9):
            c.create_rectangle(291 + col * 16, 301, 303 + col * 16, 307, fill="#95A5A6", outline="")

        c.create_rectangle(100, 250, 185, 302, fill="#6C3483", outline="#5B2C6F", width=2)
        c.create_rectangle(96, 190, 122, 258, fill="#7D3C98", outline="#6C3483", width=2)
        for lx in [105, 178]:
            c.create_line(lx, 302, lx - 5, 360, fill="#5B2C6F", width=5)

        body = [128, 252, 122, 228, 126, 206, 134, 194, 148, 188,
                172, 187, 182, 195, 185, 216, 182, 240, 180, 254]
        c.create_polygon(body, fill="#2980B9", outline="#1F618D", width=2)
        c.create_line(175, 205, 265, 262, 315, 300, fill="#2980B9", width=11, smooth=True, capstyle="round")
        c.create_line(178, 212, 270, 268, 295, 300, fill="#2980B9", width=11, smooth=True, capstyle="round")

        sx, sy = 140, 213
        c.create_oval(sx - 32, sy - 24, sx + 32, sy + 24, fill="#27AE60", outline="#1E8449", width=3)
        for angle in range(0, 360, 60):
            r = math.radians(angle)
            hx = sx + 15 * math.cos(r); hy = sy + 11 * math.sin(r)
            c.create_oval(hx - 8, hy - 6, hx + 8, hy + 6, fill="#2ECC71", outline="#27AE60", width=1)
        c.create_oval(sx - 7, sy - 6, sx + 7, sy + 6, fill="#2ECC71", outline="#27AE60", width=1)

        # 애니메이션 대상 (tag="anim")
        neck = [150, 192, 157, 180, 172, 168, 198, 157, 228, 148, 252, 144]
        c.create_line(neck, fill="#FDBCB4", width=15, smooth=True, capstyle="round", tags="anim")
        hx = 268
        c.create_oval(hx - 24, 112, hx + 24, 158, fill="#FDBCB4", outline="#E59866", width=2, tags="anim")
        c.create_arc(hx - 24, 112, hx + 24, 140, start=0, extent=180, fill="#5D4037", outline="", tags="anim")
        for ex in [hx - 12, hx + 4]:
            c.create_oval(ex, 128, ex + 14, 142, fill="white", outline="#888", width=1, tags="anim")
            c.create_oval(ex + 3, 131, ex + 10, 139, fill="#333", outline="", tags="anim")
        for angle in [25, 45, 65]:
            r = math.radians(angle)
            c.create_line(hx - 5, 130, hx - 5 - 9 * math.cos(r), 130 - 9 * math.sin(r),
                         fill="#E74C3C", width=1, tags="anim")
        c.create_arc(hx - 9, 147, hx + 9, 158, start=220, extent=100, outline="#7B4F2E", width=2, tags="anim")
        c.create_line(160, 178, 238, 152, fill="#E74C3C", width=2,
                     arrow="last", arrowshape=(10, 12, 4), tags="anim")
        c.create_text(150, 170, text="목이\n앞으로!", fill="#FF6B6B", font=("맑은 고딕", 8, "bold"), tags="anim")
        c.create_line(150, 194, 245, 152, fill="#F39C12", width=1, dash=(5, 4), tags="anim")

        for dx, dy2, col in [(1, 1, "#072C28"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx, 372 + dy2,
                         text="턱 당기자!  ·  뒤통수 벽에 붙이기  ·  목 스트레칭!",
                         fill=col, font=("맑은 고딕", 13, "bold"))
        c.create_text(W // 2, 396, text="지금 당장 턱 당기세요!  🐢 → 🧍",
                     fill="#4ECDC4", font=("맑은 고딕", 11))

    def _draw_hunched_monkey(self, c, W, H):
        """장면 2: 굽은 등  /  anim: 머리 꾸벅(anim_body) + ZZZ 둥실(anim_zzz)"""
        c.create_rectangle(0, 0, W, H, fill="#7D3C0A", outline="")
        c.create_rectangle(0, H * 0.55, W, H, fill="#5D2E08", outline="")
        c.create_oval(-30, -30, 150, 150, fill="#8B4513", outline="")
        c.create_oval(W - 100, H - 100, W + 30, H + 30, fill="#8B4513", outline="")

        for dx, dy, col in [(2, 2, "#5D2E08"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx, 40 + dy, text="🙈  굽은 등 경보!",
                         fill=col, font=("맑은 고딕", 23, "bold"))

        c.create_rectangle(65, 292, W - 65, 308, fill="#6B4226", outline="#4A2C18", width=2)
        c.create_rectangle(125, 308, 162, 375, fill="#6B4226", outline="#4A2C18", width=2)
        c.create_rectangle(328, 308, 365, 375, fill="#6B4226", outline="#4A2C18", width=2)

        c.create_rectangle(175, 238, 368, 292, fill="#1C1C1E", outline="#3A3A3C", width=2)
        c.create_rectangle(188, 248, 355, 284, fill="#2C2C2E", outline="")
        c.create_rectangle(188, 248, 355, 262, fill="#1A3A5C", outline="")
        c.create_text(272, 255, text="K&B_report.py", fill="#142E4A", font=("Consolas", 6))
        for i in range(3):
            c.create_rectangle(196, 252 + i * 8, 348, 256 + i * 8, fill="#2E86AB", outline="")
        c.create_rectangle(165, 291, 372, 302, fill="#2C2C2E", outline="#3A3A3C", width=1)
        for col in range(12):
            c.create_rectangle(170 + col * 16, 293, 180 + col * 16, 299, fill="#1C1C1E", outline="")

        tail = [218, 286, 206, 312, 184, 334, 160, 345, 145, 340]
        c.create_line(tail, fill="#C68642", width=9, smooth=True, capstyle="round")
        c.create_oval(192, 268, 255, 296, fill="#C68642", outline="#A0522D", width=2)
        c.create_line(202, 287, 190, 358, fill="#C68642", width=13, capstyle="round")
        c.create_line(245, 287, 260, 358, fill="#C68642", width=13, capstyle="round")

        body = [192, 262, 185, 240, 178, 216, 177, 198, 182, 180, 196, 170,
                260, 168, 275, 178, 280, 200, 276, 228, 268, 260, 255, 272, 192, 270]
        c.create_polygon(body, fill="#C68642", outline="#A0522D", width=2)
        c.create_oval(170, 192, 198, 232, fill="#E74C3C", outline="", stipple="gray25")
        c.create_text(152, 210, text="등!", fill="#FF6B35", font=("맑은 고딕", 13, "bold"))
        c.create_line(163, 210, 180, 212, fill="#FF6B35", width=2, arrow="last", arrowshape=(9, 11, 3))
        c.create_line(198, 183, 183, 232, 182, 268, 198, 283,
                     fill="#C68642", width=14, smooth=True, capstyle="round")
        c.create_line(262, 180, 278, 230, 280, 265, 262, 281,
                     fill="#C68642", width=14, smooth=True, capstyle="round")
        c.create_oval(178, 280, 204, 297, fill="#D2B48C", outline="#C68642", width=1)
        c.create_oval(256, 280, 282, 297, fill="#D2B48C", outline="#C68642", width=1)

        # 애니메이션 대상 — 머리 (anim_body)
        c.create_line(200, 172, 210, 155, 218, 146, fill="#C68642", width=13,
                     smooth=True, capstyle="round", tags="anim_body")
        c.create_oval(196, 116, 256, 162, fill="#C68642", outline="#A0522D", width=2, tags="anim_body")
        for ex, ex2 in [(185, 202), (250, 267)]:
            c.create_oval(ex, 126, ex2, 156, fill="#C68642", outline="#A0522D", width=2, tags="anim_body")
            c.create_oval(ex + 3, 130, ex2 - 3, 152, fill="#D2B48C", outline="", tags="anim_body")
        c.create_oval(206, 142, 246, 164, fill="#D2B48C", outline="#C68642", width=1, tags="anim_body")
        c.create_line(210, 132, 222, 134, fill="#5D4037", width=3, tags="anim_body")
        c.create_line(234, 132, 246, 134, fill="#5D4037", width=3, tags="anim_body")
        c.create_oval(216, 152, 222, 157, fill="#A0522D", outline="", tags="anim_body")
        c.create_oval(230, 152, 236, 157, fill="#A0522D", outline="", tags="anim_body")
        c.create_arc(214, 156, 238, 165, start=220, extent=100, outline="#7B4F2E", width=2, tags="anim_body")

        # 애니메이션 대상 — ZZZ (anim_zzz, 반대 방향 둥실)
        c.create_text(278, 130, text="z", fill="#CCCCCC", font=("Arial", 11, "italic"), tags="anim_zzz")
        c.create_text(292, 116, text="z", fill="#AAAAAA", font=("Arial", 13, "italic"), tags="anim_zzz")
        c.create_text(308, 100, text="Z", fill="#888888", font=("Arial", 16, "italic bold"), tags="anim_zzz")

        for dx, dy2, col in [(1, 1, "#5D2E08"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx, 372 + dy2,
                         text="어깨 허리 목!  기지개 켜고 허리 세우기!",
                         fill=col, font=("맑은 고딕", 13, "bold"))
        c.create_text(W // 2, 396, text="지금 허리 쭉 펴고 어깨 뒤로 당기세요!  🙈 → 💪",
                     fill="#F0A04B", font=("맑은 고딕", 11))

    def _draw_shoulder_reminder(self, c, W, H):
        """장면 3: 어깨 비교  /  anim: 나쁜 자세 좌우 덜덜"""
        c.create_rectangle(0, 0, W, H, fill="#1A0533", outline="")
        c.create_rectangle(0, H * 0.55, W, H, fill="#120328", outline="")
        c.create_oval(-30, -30, 140, 140, fill="#240A45", outline="")
        c.create_oval(W - 100, H - 100, W + 30, H + 30, fill="#240A45", outline="")

        rng = random.Random(42)
        for _ in range(28):
            sx = rng.randint(8, W - 8); sy = rng.randint(8, H // 2 - 10); r = rng.randint(1, 3)
            c.create_oval(sx - r, sy - r, sx + r, sy + r, fill="white", outline="")

        for dx, dy, col in [(2, 2, "#120328"), (0, 0, "#F9CA24")]:
            c.create_text(W // 2 + dx, 40 + dy, text="💪  어깨 펴기 타임!",
                         fill=col, font=("맑은 고딕", 23, "bold"))

        cx_b, cx_g, cy = 142, 378, 215
        hy = cy - 95

        # 좋은 자세 (정적)
        c.create_oval(cx_g - 78, cy - 125, cx_g + 78, cy + 125, fill="#27AE60", outline="#2ECC71", width=3)
        c.create_oval(cx_g - 18, hy - 20, cx_g + 18, hy + 20, fill="white", outline="")
        c.create_line(cx_g, hy + 20, cx_g, hy + 33, fill="white", width=5)
        c.create_line(cx_g, hy + 33, cx_g, hy + 110, fill="white", width=5)
        c.create_line(cx_g - 52, hy + 35, cx_g, hy + 35, cx_g + 52, hy + 35, fill="white", width=6)
        c.create_line(cx_g - 52, hy + 35, cx_g - 62, hy + 72, fill="white", width=4)
        c.create_line(cx_g + 52, hy + 35, cx_g + 62, hy + 72, fill="white", width=4)
        c.create_line(cx_g, hy + 110, cx_g - 18, hy + 145, fill="white", width=5)
        c.create_line(cx_g, hy + 110, cx_g + 18, hy + 145, fill="white", width=5)
        c.create_oval(cx_g - 10, hy - 10, cx_g - 3, hy - 3, fill="#27AE60", outline="")
        c.create_oval(cx_g + 3, hy - 10, cx_g + 10, hy - 3, fill="#27AE60", outline="")
        c.create_arc(cx_g - 9, hy + 5, cx_g + 9, hy + 16, start=200, extent=140, outline="#27AE60", width=2)
        c.create_line(cx_g - 32, cy + 5, cx_g - 8, cy + 35, cx_g + 42, cy - 45,
                     fill="#2ECC71", width=7, joinstyle="round", capstyle="round")
        c.create_text(cx_g, cy + 138, text="목표 자세!", fill="#2ECC71", font=("맑은 고딕", 11, "bold"))
        c.create_text(cx_g + 72, cy - 20, text="어깨\n펴짐!", fill="#A8F0C6", font=("맑은 고딕", 8))
        c.create_text(cx_g, cy + 52, text="K&B", fill="#1E8449", font=("Arial", 6, "bold"))

        mid_x = (cx_b + cx_g) // 2
        c.create_line(cx_b + 82, cy, cx_g - 82, cy,
                     fill="#F9CA24", width=5, arrow="last", arrowshape=(20, 25, 8))
        c.create_text(mid_x, cy - 24, text="이렇게!", fill="#F9CA24", font=("맑은 고딕", 13, "bold"))

        # 나쁜 자세 (애니메이션 — 좌우 덜덜)
        c.create_oval(cx_b - 78, cy - 125, cx_b + 78, cy + 125,
                     fill="#C0392B", outline="#E74C3C", width=3, tags="anim")
        c.create_oval(cx_b - 2, hy - 20, cx_b + 22, hy + 20, fill="white", outline="", tags="anim")
        c.create_line(cx_b + 10, hy + 20, cx_b + 6, hy + 32, fill="white", width=5, tags="anim")
        spine_b = [cx_b + 6, hy + 32, cx_b + 2, hy + 52, cx_b - 8, hy + 70,
                   cx_b - 4, hy + 90, cx_b, hy + 110]
        c.create_line(spine_b, fill="white", width=5, smooth=True, tags="anim")
        c.create_line(cx_b - 52, hy + 38, cx_b + 6, hy + 34, cx_b + 52, hy + 40,
                     fill="white", width=6, smooth=True, tags="anim")
        c.create_line(cx_b - 52, hy + 38, cx_b - 58, hy + 80, fill="white", width=4, tags="anim")
        c.create_line(cx_b + 52, hy + 40, cx_b + 58, hy + 80, fill="white", width=4, tags="anim")
        c.create_line(cx_b, hy + 110, cx_b - 18, hy + 145, fill="white", width=5, tags="anim")
        c.create_line(cx_b, hy + 110, cx_b + 18, hy + 145, fill="white", width=5, tags="anim")
        c.create_arc(cx_b + 2, hy + 3, cx_b + 20, hy + 14,
                    start=30, extent=120, outline="#C0392B", width=2, tags="anim")
        c.create_line(cx_b - 55, cy - 110, cx_b + 55, cy + 110, fill="#E74C3C", width=4, tags="anim")
        c.create_line(cx_b + 55, cy - 110, cx_b - 55, cy + 110, fill="#E74C3C", width=4, tags="anim")
        c.create_text(cx_b, cy + 138, text="지금 나의 모습",
                     fill="#E74C3C", font=("맑은 고딕", 11, "bold"), tags="anim")
        c.create_text(cx_b - 5, cy - 8, text="어깨\n처짐", fill="#FFB3B3", font=("맑은 고딕", 8), tags="anim")

        for dx, dy2, col in [(1, 1, "#120328"), (0, 0, "#F9CA24")]:
            c.create_text(W // 2 + dx, 372 + dy2,
                         text="어깨 피고 가슴 열기!  척추 쭉 세우기!",
                         fill=col, font=("맑은 고딕", 13, "bold"))
        c.create_text(W // 2, 396, text="10초만 기지개 켜세요 ✨  어깨 뒤로, 가슴 앞으로!",
                     fill="#A29BFE", font=("맑은 고딕", 11))

    def _draw_banzai_stretch(self, c, W, H):
        """장면 4: 만세 스트레칭  /  anim: 팔 위아래"""
        c.create_rectangle(0, 0, W, H, fill="#0B3D2E", outline="")
        c.create_rectangle(0, H * 0.55, W, H, fill="#072C20", outline="")
        c.create_oval(-40, -40, 170, 170, fill="#145239", outline="")
        c.create_oval(W - 130, H - 130, W + 50, H + 50, fill="#145239", outline="")

        for dx, dy, col in [(2, 2, "#072C20"), (0, 0, "#F0FFF4")]:
            c.create_text(W // 2 + dx, 40 + dy, text="🙆  만세 스트레칭!",
                         fill=col, font=("맑은 고딕", 23, "bold"))

        cx, cy = W // 2, 245
        c.create_oval(cx - 50, cy + 143, cx + 50, cy + 156, fill="#072C20", outline="")

        c.create_line(cx, cy + 55, cx - 22, cy + 143, fill="#27AE60", width=8, capstyle="round")
        c.create_line(cx, cy + 55, cx + 22, cy + 143, fill="#27AE60", width=8, capstyle="round")
        c.create_oval(cx - 33, cy + 135, cx - 9, cy + 153, fill="#1E8449", outline="")
        c.create_oval(cx + 9, cy + 135, cx + 33, cy + 153, fill="#1E8449", outline="")
        c.create_line(cx, cy - 32, cx, cy + 57, fill="#27AE60", width=10, capstyle="round")
        c.create_text(cx + 14, cy + 8, text="K&B", fill="#1A7A42", font=("Arial", 6, "bold"))

        c.create_oval(cx - 28, cy - 94, cx + 28, cy - 38, fill="#FDBCB4", outline="#E59866", width=2)
        c.create_arc(cx - 28, cy - 94, cx + 28, cy - 64, start=0, extent=180, fill="#5D4037", outline="")
        c.create_arc(cx - 16, cy - 78, cx - 3, cy - 66, start=10, extent=160, outline="#333", width=2)
        c.create_arc(cx + 3, cy - 78, cx + 16, cy - 66, start=10, extent=160, outline="#333", width=2)
        c.create_arc(cx - 13, cy - 60, cx + 13, cy - 45, start=200, extent=140, outline="#E74C3C", width=3)
        c.create_oval(cx - 23, cy - 66, cx - 10, cy - 55, fill="#FCA5A5", outline="")
        c.create_oval(cx + 10, cy - 66, cx + 23, cy - 55, fill="#FCA5A5", outline="")

        c.create_line(cx - 4, cy - 22, cx - 54, cy - 68, fill="#27AE60", width=9, capstyle="round", tags="anim")
        c.create_line(cx - 54, cy - 68, cx - 62, cy - 122, fill="#27AE60", width=8, capstyle="round", tags="anim")
        c.create_oval(cx - 73, cy - 134, cx - 52, cy - 113, fill="#FDBCB4", outline="", tags="anim")
        c.create_line(cx + 4, cy - 22, cx + 54, cy - 68, fill="#27AE60", width=9, capstyle="round", tags="anim")
        c.create_line(cx + 54, cy - 68, cx + 62, cy - 122, fill="#27AE60", width=8, capstyle="round", tags="anim")
        c.create_oval(cx + 52, cy - 134, cx + 73, cy - 113, fill="#FDBCB4", outline="", tags="anim")
        c.create_text(cx - 82, cy - 145, text="✨", font=("Arial", 14), tags="anim")
        c.create_text(cx + 82, cy - 145, text="✨", font=("Arial", 14), tags="anim")
        c.create_text(cx, cy - 150, text="⬆", fill="#FFE66D", font=("Arial", 18, "bold"), tags="anim")

        for dx2, dy2, col in [(1, 1, "#072C20"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx2, 372 + dy2,
                         text="양팔 번쩍!  온몸을 위로 쭉 늘려보세요!",
                         fill=col, font=("맑은 고딕", 13, "bold"))
        c.create_text(W // 2, 396, text="10초만 기지개 켜도 몸이 달라져요  🙆",
                     fill="#4ECDC4", font=("맑은 고딕", 11))

    def _draw_neck_tilt(self, c, W, H):
        """장면 5: 목 옆 스트레칭  /  anim: 목+머리 좌우 틸트"""
        c.create_rectangle(0, 0, W, H, fill="#0A2535", outline="")
        c.create_rectangle(0, H * 0.55, W, H, fill="#061822", outline="")
        c.create_oval(-40, -40, 180, 180, fill="#0F3048", outline="")
        c.create_oval(W - 140, H - 140, W + 50, H + 50, fill="#0F3048", outline="")

        for dx, dy, col in [(2, 2, "#061822"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx, 40 + dy, text="🦒  목 옆으로 기울이기!",
                         fill=col, font=("맑은 고딕", 23, "bold"))

        cx, cy = W // 2, 248
        c.create_oval(cx - 50, cy + 138, cx + 50, cy + 152, fill="#061822", outline="")

        c.create_line(cx, cy + 55, cx - 22, cy + 140, fill="#E67E22", width=8, capstyle="round")
        c.create_line(cx, cy + 55, cx + 22, cy + 140, fill="#E67E22", width=8, capstyle="round")
        c.create_oval(cx - 33, cy + 132, cx - 9, cy + 150, fill="#D35400", outline="")
        c.create_oval(cx + 9, cy + 132, cx + 33, cy + 150, fill="#D35400", outline="")
        c.create_line(cx, cy - 28, cx, cy + 57, fill="#E67E22", width=10, capstyle="round")
        c.create_line(cx, cy + 5, cx - 46, cy + 22, fill="#E67E22", width=7, capstyle="round")
        c.create_line(cx, cy + 5, cx + 46, cy + 22, fill="#E67E22", width=7, capstyle="round")
        c.create_oval(cx - 57, cy + 16, cx - 38, cy + 33, fill="#FDBCB4", outline="")
        c.create_oval(cx + 38, cy + 16, cx + 57, cy + 33, fill="#FDBCB4", outline="")

        c.create_text(cx - 118, cy - 55, text="좌우\n각 10초",
                     fill="#4ECDC4", font=("맑은 고딕", 9, "bold"))
        c.create_text(cx - 118, cy - 20, text="← →", fill="#4ECDC4", font=("Arial", 20, "bold"))
        c.create_text(W - 22, H - 20, text="K&B", fill="#0C2035", font=("Arial", 7, "bold"))

        neck = [cx, cy - 28, cx + 4, cy - 52, cx + 10, cy - 76, cx + 14, cy - 92]
        c.create_line(neck, fill="#FDBCB4", width=14, smooth=True, capstyle="round", tags="anim")
        c.create_oval(cx - 14, cy - 132, cx + 44, cy - 88, fill="#FDBCB4", outline="#E59866", width=2, tags="anim")
        c.create_arc(cx - 14, cy - 132, cx + 44, cy - 106, start=0, extent=180, fill="#2C3E50", outline="", tags="anim")
        c.create_oval(cx - 8, cy - 120, cx + 5, cy - 108, fill="white", outline="#888", width=1, tags="anim")
        c.create_oval(cx + 10, cy - 116, cx + 23, cy - 104, fill="white", outline="#888", width=1, tags="anim")
        c.create_oval(cx - 6, cy - 118, cx + 1, cy - 111, fill="#333", outline="", tags="anim")
        c.create_oval(cx + 12, cy - 114, cx + 19, cy - 107, fill="#333", outline="", tags="anim")
        c.create_arc(cx - 4, cy - 100, cx + 26, cy - 90, start=210, extent=120, outline="#E74C3C", width=2, tags="anim")
        c.create_oval(cx + 44, cy - 126, cx + 52, cy - 115, fill="#60A5FA", outline="", tags="anim")
        c.create_text(cx + 62, cy - 108, text="쭉!", fill="#F39C12",
                     font=("맑은 고딕", 10, "bold"), tags="anim")

        for dx2, dy2, col in [(1, 1, "#061822"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx2, 372 + dy2,
                         text="귀를 어깨쪽으로!  좌우 각 10초씩!",
                         fill=col, font=("맑은 고딕", 13, "bold"))
        c.create_text(W // 2, 396, text="목 긴장 풀기!  천천히 좌우로  🦒",
                     fill="#60A5FA", font=("맑은 고딕", 11))

    def _draw_shoulder_shrug(self, c, W, H):
        """장면 6: 어깨 으쓱 스트레칭  /  anim: 어깨 위아래"""
        c.create_rectangle(0, 0, W, H, fill="#1B0A35", outline="")
        c.create_rectangle(0, H * 0.55, W, H, fill="#130228", outline="")
        c.create_oval(-40, -40, 170, 170, fill="#240A48", outline="")
        c.create_oval(W - 130, H - 130, W + 50, H + 50, fill="#240A48", outline="")

        rng2 = random.Random(88)
        for _ in range(20):
            sx = rng2.randint(8, W - 8); sy = rng2.randint(8, H // 2 - 10); r = rng2.randint(1, 3)
            c.create_oval(sx - r, sy - r, sx + r, sy + r, fill="#A78BFA", outline="")
        c.create_text(448, 62, text="K&B", fill="#2A1055", font=("Arial", 7))

        for dx, dy, col in [(2, 2, "#130228"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx, 40 + dy, text="💜  어깨 으쓱 스트레칭!",
                         fill=col, font=("맑은 고딕", 23, "bold"))

        cx, cy = W // 2, 248
        c.create_oval(cx - 52, cy + 140, cx + 52, cy + 155, fill="#130228", outline="")

        c.create_line(cx, cy + 55, cx - 22, cy + 142, fill="#8B5CF6", width=8, capstyle="round")
        c.create_line(cx, cy + 55, cx + 22, cy + 142, fill="#8B5CF6", width=8, capstyle="round")
        c.create_oval(cx - 33, cy + 134, cx - 9, cy + 152, fill="#7C3AED", outline="")
        c.create_oval(cx + 9, cy + 134, cx + 33, cy + 152, fill="#7C3AED", outline="")
        c.create_line(cx, cy + 12, cx, cy + 57, fill="#8B5CF6", width=10, capstyle="round")

        c.create_oval(cx - 28, cy - 94, cx + 28, cy - 38, fill="#FDBCB4", outline="#E59866", width=2)
        c.create_arc(cx - 28, cy - 94, cx + 28, cy - 64, start=0, extent=180, fill="#1E3A5F", outline="")
        for ex in [cx - 14, cx + 3]:
            c.create_oval(ex, cy - 80, ex + 15, cy - 66, fill="white", outline="#888", width=1)
            c.create_oval(ex + 3, cy - 77, ex + 11, cy - 70, fill="#333", outline="")
        c.create_arc(cx - 12, cy - 57, cx + 12, cy - 45, start=200, extent=140, outline="#E74C3C", width=2)
        c.create_oval(cx - 23, cy - 64, cx - 11, cy - 54, fill="#FCA5A5", outline="")
        c.create_oval(cx + 11, cy - 64, cx + 23, cy - 54, fill="#FCA5A5", outline="")

        c.create_line(cx, cy + 10, cx - 58, cy - 14, fill="#8B5CF6", width=12, capstyle="round", tags="anim")
        c.create_line(cx - 58, cy - 14, cx - 62, cy + 36, fill="#8B5CF6", width=9, capstyle="round", tags="anim")
        c.create_oval(cx - 73, cy + 28, cx - 52, cy + 47, fill="#FDBCB4", outline="", tags="anim")
        c.create_line(cx, cy + 10, cx + 58, cy - 14, fill="#8B5CF6", width=12, capstyle="round", tags="anim")
        c.create_line(cx + 58, cy - 14, cx + 62, cy + 36, fill="#8B5CF6", width=9, capstyle="round", tags="anim")
        c.create_oval(cx + 52, cy + 28, cx + 73, cy + 47, fill="#FDBCB4", outline="", tags="anim")
        c.create_text(cx - 76, cy - 26, text="↑", fill="#C4B5FD", font=("Arial", 16, "bold"), tags="anim")
        c.create_text(cx + 76, cy - 26, text="↑", fill="#C4B5FD", font=("Arial", 16, "bold"), tags="anim")
        c.create_text(cx, cy - 30, text="으쓱!", fill="#E9D5FF",
                     font=("맑은 고딕", 11, "bold"), tags="anim")

        for dx2, dy2, col in [(1, 1, "#130228"), (0, 0, "#FFE66D")]:
            c.create_text(W // 2 + dx2, 372 + dy2,
                         text="어깨 귀까지 으쓱!  힘 빼고 툭 내려요!",
                         fill=col, font=("맑은 고딕", 13, "bold"))
        c.create_text(W // 2, 396, text="10회 반복!  어깨 긴장 싹 풀기  💜",
                     fill="#C4B5FD", font=("맑은 고딕", 11))

    # ── 갤러리 ─────────────────────────────────────────────────────────────

    def _show_gallery(self):
        # self.scenes 기반 — 장면이 추가되면 자동으로 반영됨
        all_scenes = self.scenes  # 새 장면은 여기에 append하면 됨
        total = len(all_scenes)
        idx = [0]
        anim_job = [None]

        W, H = 520, 460
        popup = self._make_popup(W, H)

        BG_NAV = "#111122"
        canvas = tk.Canvas(popup, width=W, height=H - 44,
                           highlightthickness=0, cursor="hand2")
        canvas.pack(fill="both", expand=True)

        nav = tk.Frame(popup, bg=BG_NAV, height=44)
        nav.pack(fill="x", side="bottom")
        nav.pack_propagate(False)

        counter_var = tk.StringVar()

        def cancel_anim():
            if anim_job[0] is not None:
                try:
                    popup.after_cancel(anim_job[0])
                except Exception:
                    pass
                anim_job[0] = None

        def render(i):
            cancel_anim()
            canvas.delete("all")
            draw_fn = all_scenes[i]
            draw_fn(canvas, W, H - 44)
            counter_var.set(f"{i + 1}  /  {total}")

            cfg = ANIM_CONFIGS[i] if i < len(ANIM_CONFIGS) else []
            if cfg:
                frame_n = [0]
                def tick(fn=frame_n, c=cfg):
                    if not popup.winfo_exists():
                        return
                    sign = 1 if fn[0] % 2 == 0 else -1
                    for tag, dx, dy in c:
                        canvas.move(tag, dx * sign, dy * sign)
                    fn[0] += 1
                    anim_job[0] = popup.after(ANIM_INTERVAL_MS, tick)
                anim_job[0] = popup.after(ANIM_INTERVAL_MS, tick)

        def go(delta):
            idx[0] = (idx[0] + delta) % total
            render(idx[0])

        ACC = "#4ECDC4"
        btn_cfg = dict(bg=BG_NAV, fg=ACC, font=("맑은 고딕", 16, "bold"),
                       relief="flat", bd=0, cursor="hand2",
                       activebackground=BG_NAV, activeforeground="white")

        tk.Button(nav, text="◀", command=lambda: go(-1), **btn_cfg).pack(side="left", padx=20)
        tk.Label(nav, textvariable=counter_var, bg=BG_NAV, fg="#AAAACC",
                 font=("맑은 고딕", 11)).pack(side="left", expand=True)
        tk.Button(nav, text="▶", command=lambda: go(+1), **btn_cfg).pack(side="right", padx=20)

        tk.Button(nav, text="✕", command=popup.destroy,
                  bg=BG_NAV, fg="#555566", font=("맑은 고딕", 11),
                  relief="flat", bd=0, cursor="hand2",
                  activebackground=BG_NAV, activeforeground="#AAAACC").place(relx=1.0, rely=0.0,
                                                                              anchor="ne", x=-6, y=4)

        render(0)

        def fade(a=0.0):
            if not popup.winfo_exists():
                return
            a = min(a + 0.1, 0.95)
            popup.attributes("-alpha", a)
            if a < 0.95:
                popup.after(25, lambda: fade(a))
        fade()

    # ── 설정 창 ────────────────────────────────────────────────────────────

    def _show_settings_window(self):
        monitors = self._get_monitors()
        win_h = 450 + (len(monitors) + 1) * 44

        win = tk.Toplevel(self.root)
        win.title("자세 알리미 — 환경 설정")
        win.geometry(f"400x{win_h}")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.configure(bg="#1A1A2E")

        win.update_idletasks()
        primary = next((m for m in monitors if getattr(m, "is_primary", False)), monitors[0])
        sx = primary.x + (primary.width - 400) // 2
        sy = primary.y + (primary.height - win_h) // 2
        win.geometry(f"400x{win_h}+{sx}+{sy}")

        BG, FG, ACC, DIM = "#1A1A2E", "#EAEAEA", "#4ECDC4", "#555566"

        tk.Label(win, text="자세 알리미 환경 설정", bg=BG, fg=ACC,
                font=("맑은 고딕", 15, "bold")).pack(pady=(18, 4))

        # ── 마지막/다음 알림 상태 ────────────────────────────────────────
        def _fmt_time(ts: float) -> str:
            return datetime.fromtimestamp(ts).strftime("%H:%M")

        def _minutes_until(ts: float) -> str:
            remaining = int((ts - time.time()) / 60)
            if remaining <= 0:
                return "곧"
            return f"{remaining}분 후"

        status_frame = tk.Frame(win, bg="#16213E", bd=0)
        status_frame.pack(fill="x", padx=24, pady=(4, 8))
        status_frame.columnconfigure(0, minsize=92)
        status_frame.columnconfigure(1, weight=1)

        if self.last_notified is not None:
            last_str = _fmt_time(self.last_notified)
            next_ts = self.last_notified + self.interval_minutes * 60
        else:
            last_str = "최초 실행"
            next_ts = self.last_shown + self.interval_minutes * 60

        next_str = f"{_fmt_time(next_ts)}  ({_minutes_until(next_ts)})"

        for i, (label_text, value_text) in enumerate([("마지막 알림", last_str), ("다음 알림", next_str)]):
            tk.Label(status_frame, text=label_text, bg="#16213E", fg="#888",
                     font=("맑은 고딕", 9), anchor="w", padx=14, pady=5).grid(row=i, column=0, sticky="w")
            tk.Label(status_frame, text=value_text, bg="#16213E", fg=ACC,
                     font=("맑은 고딕", 10, "bold"), anchor="w").grid(row=i, column=1, sticky="w")

        # ── 시간 설정 ────────────────────────────────────────────────────
        def make_row(label, var, lo, hi, unit):
            f = tk.Frame(win, bg=BG)
            f.pack(fill="x", padx=30, pady=5)
            tk.Label(f, text=label, bg=BG, fg=FG,
                    font=("맑은 고딕", 11), width=9, anchor="w").pack(side="left")
            sp = tk.Spinbox(f, from_=lo, to=hi, textvariable=var, width=6,
                           bg="#16213E", fg=FG, font=("맑은 고딕", 11),
                           buttonbackground="#0F3460", relief="flat", insertbackground=FG)
            sp.pack(side="left", padx=8)
            tk.Label(f, text=unit, bg=BG, fg="#999", font=("맑은 고딕", 10)).pack(side="left")

        iv = tk.IntVar(value=self.interval_minutes)
        dv = tk.IntVar(value=self.duration_seconds)
        make_row("알림 간격:", iv, 1, 180, "분마다 알림")
        make_row("표시 시간:", dv, 2, 60, "초 동안 표시")

        # ── 자동 실행 ────────────────────────────────────────────────────
        tk.Frame(win, bg=DIM, height=1).pack(fill="x", padx=20, pady=(10, 6))

        auto_var = tk.BooleanVar(value=self._is_autostart())
        auto_frame = tk.Frame(win, bg=BG)
        auto_frame.pack(fill="x", padx=26, pady=2)
        tk.Checkbutton(
            auto_frame, text="Windows 시작 시 자동 실행",
            variable=auto_var,
            bg=BG, fg=FG, selectcolor="#0F3460",
            activebackground=BG, activeforeground=FG,
            font=("맑은 고딕", 10),
        ).pack(side="left")

        # ── 모니터 선택 ──────────────────────────────────────────────────
        tk.Frame(win, bg=DIM, height=1).pack(fill="x", padx=20, pady=(10, 6))
        tk.Label(win, text="표시 모니터", bg=BG, fg=ACC,
                font=("맑은 고딕", 11, "bold")).pack(anchor="w", padx=30)

        monitor_var = tk.IntVar(value=self.target_monitor_index)

        # 모든 모니터 옵션
        all_row = tk.Frame(win, bg=BG)
        all_row.pack(fill="x", padx=28, pady=3)
        tk.Radiobutton(
            all_row, text="모든 모니터  (동시 표시)",
            variable=monitor_var, value=ALL_MONITORS,
            bg=BG, fg=FG, selectcolor="#0F3460",
            activebackground=BG, activeforeground=FG, font=("맑은 고딕", 10),
        ).pack(side="left")

        def do_preview_all():
            monitor_var.set(ALL_MONITORS)
            self._show_reminder(monitor_index=ALL_MONITORS, preview=True)

        tk.Button(all_row, text="미리보기", command=do_preview_all,
                 bg="#2C2C4E", fg=ACC, font=("맑은 고딕", 9),
                 relief="flat", padx=8, pady=2, cursor="hand2",
                 activebackground="#3A3A60", activeforeground=ACC).pack(side="right")

        # 개별 모니터 옵션
        for i, mon in enumerate(monitors):
            primary_tag = "  (주 디스플레이)" if getattr(mon, "is_primary", False) else ""
            row = tk.Frame(win, bg=BG)
            row.pack(fill="x", padx=28, pady=3)
            tk.Radiobutton(
                row, text=f"모니터 {i + 1}    {mon.width}×{mon.height}{primary_tag}",
                variable=monitor_var, value=i,
                bg=BG, fg=FG, selectcolor="#0F3460",
                activebackground=BG, activeforeground=FG, font=("맑은 고딕", 10),
            ).pack(side="left")

            def do_preview(idx=i):
                monitor_var.set(idx)
                self._show_reminder(monitor_index=idx, preview=True)

            tk.Button(row, text="미리보기", command=do_preview,
                     bg="#2C2C4E", fg=ACC, font=("맑은 고딕", 9),
                     relief="flat", padx=8, pady=2, cursor="hand2",
                     activebackground="#3A3A60", activeforeground=ACC).pack(side="right")

        # ── 저장 ─────────────────────────────────────────────────────────
        tk.Frame(win, bg=DIM, height=1).pack(fill="x", padx=20, pady=(10, 4))

        info = tk.Label(win,
                       text=f"현재: {self.interval_minutes}분 / {self.duration_seconds}초 / 모니터 {self.target_monitor_index + 1}",
                       bg=BG, fg="#666", font=("맑은 고딕", 9))
        info.pack(pady=2)

        def save():
            self.interval_minutes = max(1, iv.get())
            self.duration_seconds = max(2, dv.get())
            self.target_monitor_index = monitor_var.get()
            self.last_shown = time.time()
            self._set_autostart(auto_var.get())
            self._save_settings()
            info.config(
                text=f"저장됨!  {self.interval_minutes}분 / {self.duration_seconds}초 / 모니터 {self.target_monitor_index + 1}",
                fg=ACC)
            win.after(700, win.destroy)

        tk.Button(win, text="  저장  ", command=save,
                 bg=ACC, fg="#1A1A2E", font=("맑은 고딕", 12, "bold"),
                 relief="flat", padx=20, pady=8, cursor="hand2",
                 activebackground="#3ABAB4", activeforeground="#1A1A2E").pack(pady=10)

        tk.Label(win, text=f"made by BTS  ·  2026.05.21  v{VERSION}",
                 bg=BG, fg="#555577", font=("맑은 고딕", 8)).pack(pady=(0, 10))


def main():
    ensure_single_instance()
    app = PostureReminder()
    app.start()


if __name__ == "__main__":
    main()
