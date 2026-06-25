#!/usr/bin/env python3
"""
Claude Auto-Approve
Claude Code 권한 다이얼로그 자동 승인 + 자율 모드 (토큰 초기화 후 자동 재개)
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import time
import subprocess
import sys
import os
from datetime import datetime
from PIL import Image, ImageTk, ImageGrab, ImageChops
import pytesseract

# ─── OCR 엔진 경로 (Homebrew 기본 경로) ─────────────────────────
pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'

# ─── 감지 패턴 ───────────────────────────────────────────────────
DIALOG_PATTERNS = [
    'do you want to proceed',
    '1. yes',
    ') 1.',
    'esc to cancel',
    'bash command',
    'allow this',
]

RATE_LIMIT_PATTERNS = [
    'rate limit',
    'usage limit',
    'try again',
    'claude is at capacity',
    'too many requests',
    'overloaded',
    'quota exceeded',
    '5 hours',
    'hour limit',
]

# ─── 다크 테마 색상 ───────────────────────────────────────────────
BG       = '#0f0f1a'
BG2      = '#1a1a2e'
BG3      = '#16213e'
ACCENT   = '#00d4ff'
GREEN    = '#00ff88'
YELLOW   = '#f39c12'
RED      = '#e74c3c'
GRAY     = '#888888'
WHITE    = '#e8e8e8'


class ScreenRegionSelector:
    """전체화면 반투명 오버레이로 영역 드래그 선택"""

    def __init__(self, parent, callback):
        self.callback = callback
        self.start_x = self.start_y = 0
        self.rect = None

        # 부모 숨김 후 오버레이 표시
        parent.withdraw()
        time.sleep(0.3)

        self.win = tk.Toplevel()
        self.win.attributes('-fullscreen', True)
        self.win.attributes('-alpha', 0.35)
        self.win.attributes('-topmost', True)
        self.win.configure(bg='#000020')

        self.canvas = tk.Canvas(self.win, bg='#000020', cursor='crosshair',
                                highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        sw = self.win.winfo_screenwidth()
        self.canvas.create_text(sw // 2, 40,
                                text='드래그하여 Claude Code 터미널 영역을 선택하세요   (ESC: 취소)',
                                fill=ACCENT, font=('SF Pro Display', 14, 'bold'))

        self.canvas.bind('<Button-1>', self._press)
        self.canvas.bind('<B1-Motion>', self._drag)
        self.canvas.bind('<ButtonRelease-1>', self._release)
        self.win.bind('<Escape>', lambda _: self._cancel(parent))

    def _press(self, e):
        self.start_x, self.start_y = e.x, e.y

    def _drag(self, e):
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, e.x, e.y,
            outline=ACCENT, width=2, fill=ACCENT, stipple='gray12')

    def _release(self, e, *_):
        x1 = min(self.start_x, e.x)
        y1 = min(self.start_y, e.y)
        x2 = max(self.start_x, e.x)
        y2 = max(self.start_y, e.y)
        self.win.destroy()

        # 부모 복원은 콜백에서 처리
        if x2 - x1 > 20 and y2 - y1 > 20:
            self.callback((x1, y1, x2 - x1, y2 - y1))
        else:
            self.callback(None)

    def _cancel(self, parent):
        self.win.destroy()
        parent.deiconify()
        self.callback(None)


class ClaudeAutoApprove:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Claude Auto-Approve')
        self.root.geometry('520x720')
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        # 상태
        self.region = None          # (x, y, w, h)
        self.monitoring = False
        self.autonomous_mode = False
        self.rate_limit_hit = False
        self.rate_limit_at = None
        self.prev_img = None
        self._monitor_thread = None
        self._auto_timer_thread = None
        self._response_after_id = None

        self._build_ui()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ──────────────────── UI 구성 ────────────────────────────────

    def _build_ui(self):
        # 헤더
        hdr = tk.Frame(self.root, bg=BG2, height=56)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text='Claude Auto-Approve',
                 font=('SF Pro Display', 17, 'bold'),
                 bg=BG2, fg=ACCENT).pack(side=tk.LEFT, padx=18, pady=14)
        self.version_lbl = tk.Label(hdr, text='v1.0', font=('SF Pro Display', 10),
                                    bg=BG2, fg=GRAY)
        self.version_lbl.pack(side=tk.RIGHT, padx=18)

        pad = dict(padx=14, pady=5)

        # ── 영역 선택 ────────────────────────────────────────────
        sec1 = self._section('모니터링 영역')
        sec1.pack(fill=tk.X, **pad)

        row = tk.Frame(sec1, bg=BG3)
        row.pack(fill=tk.X, padx=8, pady=6)

        self._btn(row, '  영역 선택', self._pick_region,
                  bg='#0f3460').pack(side=tk.LEFT)
        self.region_lbl = tk.Label(row, text='선택된 영역 없음',
                                   bg=BG3, fg=GRAY, font=('SF Pro Display', 10))
        self.region_lbl.pack(side=tk.LEFT, padx=12)

        self.preview = tk.Canvas(sec1, width=490, height=130,
                                 bg='#0a0a15', highlightthickness=1,
                                 highlightbackground='#2a2a4a')
        self.preview.pack(padx=8, pady=(0, 8))
        self.preview.create_text(245, 65, text='영역 선택 후 실시간 미리보기',
                                 fill=GRAY, font=('SF Pro Display', 11))

        # ── 상태 ─────────────────────────────────────────────────
        sec2 = self._section('상태')
        sec2.pack(fill=tk.X, **pad)

        self.status_dot = tk.Label(sec2, text='●  대기 중',
                                   font=('SF Pro Display', 13, 'bold'),
                                   bg=BG3, fg=GRAY)
        self.status_dot.pack(pady=8)

        self.detect_lbl = tk.Label(sec2, text='',
                                   font=('SF Pro Display', 10),
                                   bg=BG3, fg=YELLOW)
        self.detect_lbl.pack(pady=(0, 6))

        # ── 제어 ─────────────────────────────────────────────────
        sec3 = self._section('제어')
        sec3.pack(fill=tk.X, **pad)

        self.start_btn = self._btn(sec3, '▶  모니터 시작', self._toggle_monitor,
                                   bg='#1a5c38', fg=GREEN, size=12, bold=True, pady=9)
        self.start_btn.pack(fill=tk.X, padx=8, pady=6)

        # 응답 지연
        delay_row = tk.Frame(sec3, bg=BG3)
        delay_row.pack(fill=tk.X, padx=8, pady=(0, 4))
        tk.Label(delay_row, text='응답 지연 (초):', bg=BG3, fg=GRAY,
                 font=('SF Pro Display', 10)).pack(side=tk.LEFT)
        self.delay_var = tk.StringVar(value='1.0')
        delay_entry = tk.Entry(delay_row, textvariable=self.delay_var, width=5,
                               bg=BG2, fg=WHITE, insertbackground=WHITE,
                               font=('SF Pro Display', 10), relief=tk.FLAT)
        delay_entry.pack(side=tk.LEFT, padx=6)
        tk.Label(delay_row, text='(0 = 즉시 응답)', bg=BG3, fg=GRAY,
                 font=('SF Pro Display', 9)).pack(side=tk.LEFT)

        # 구분선
        tk.Frame(sec3, bg='#2a2a4a', height=1).pack(fill=tk.X, padx=8, pady=6)

        # 자율 모드
        self.auto_var = tk.BooleanVar()
        auto_chk = tk.Checkbutton(sec3,
                                   text='  자율 모드  —  토큰 소진 시 초기화 후 자동 재개',
                                   variable=self.auto_var,
                                   command=self._toggle_autonomous,
                                   bg=BG3, fg=WHITE, selectcolor=BG2,
                                   activebackground=BG3, activeforeground=WHITE,
                                   font=('SF Pro Display', 10),
                                   relief=tk.FLAT, bd=0)
        auto_chk.pack(anchor=tk.W, padx=8, pady=4)

        resume_row = tk.Frame(sec3, bg=BG3)
        resume_row.pack(fill=tk.X, padx=8, pady=(0, 6))
        tk.Label(resume_row, text='재개 명령어:', bg=BG3, fg=GRAY,
                 font=('SF Pro Display', 10)).pack(side=tk.LEFT)
        self.resume_var = tk.StringVar(value='개발 계속해줘')
        tk.Entry(resume_row, textvariable=self.resume_var, width=28,
                 bg=BG2, fg=WHITE, insertbackground=WHITE,
                 font=('SF Pro Display', 10), relief=tk.FLAT).pack(side=tk.LEFT, padx=6)

        self.auto_status_lbl = tk.Label(sec3, text='',
                                        font=('SF Pro Display', 10, 'bold'),
                                        bg=BG3, fg=YELLOW)
        self.auto_status_lbl.pack(pady=(0, 6))

        # ── 로그 ─────────────────────────────────────────────────
        sec4 = self._section('로그')
        sec4.pack(fill=tk.BOTH, expand=True, **pad)

        self.log_box = scrolledtext.ScrolledText(
            sec4, height=9, state=tk.DISABLED,
            bg='#080810', fg=GREEN,
            font=('Menlo', 9), relief=tk.FLAT,
            insertbackground=GREEN)
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.log_box.tag_config('warn',  foreground=YELLOW)
        self.log_box.tag_config('error', foreground=RED)
        self.log_box.tag_config('ok',    foreground=GREEN)

        self._log('Claude Auto-Approve 시작', 'ok')
        self._log('영역을 선택하고 모니터를 시작하세요.')

    def _section(self, title):
        frame = tk.LabelFrame(self.root, text=f'  {title}  ',
                              bg=BG3, fg=ACCENT,
                              font=('SF Pro Display', 10, 'bold'),
                              relief=tk.GROOVE, bd=1,
                              labelanchor='nw')
        return frame

    def _btn(self, parent, text, cmd, bg='#1a1a3e', fg=WHITE,
             size=10, bold=False, pady=6):
        weight = 'bold' if bold else 'normal'
        b = tk.Button(parent, text=text, command=cmd,
                      bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                      font=('SF Pro Display', size, weight),
                      relief=tk.FLAT, bd=0, pady=pady,
                      cursor='hand2')
        return b

    # ──────────────────── 로그 ──────────────────────────────────

    def _log(self, msg, tag=''):
        ts = datetime.now().strftime('%H:%M:%S')
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, f'[{ts}] {msg}\n', tag)
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    # ──────────────────── 영역 선택 ─────────────────────────────

    def _pick_region(self):
        ScreenRegionSelector(self.root, self._on_region)

    def _on_region(self, region):
        self.root.deiconify()
        if region is None:
            return
        self.region = region
        x, y, w, h = region
        self.region_lbl.config(text=f'({x}, {y})  {w} × {h} px', fg=ACCENT)
        self._log(f'영역 설정: ({x}, {y}) {w}×{h}', 'ok')
        self._refresh_preview()

    def _refresh_preview(self):
        if not self.region:
            return
        try:
            x, y, w, h = self.region
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            img.thumbnail((490, 130), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.preview.delete('all')
            pw, ph = self.preview.winfo_width() or 490, self.preview.winfo_height() or 130
            self.preview.create_image(pw // 2, ph // 2, image=photo, anchor='center')
            self.preview.image = photo
        except Exception:
            pass

    # ──────────────────── 모니터링 제어 ─────────────────────────

    def _toggle_monitor(self):
        if self.monitoring:
            self._stop_monitor()
        else:
            self._start_monitor()

    def _start_monitor(self):
        if not self.region:
            messagebox.showwarning('영역 미선택', '먼저 모니터링 영역을 선택하세요.')
            return
        self.monitoring = True
        self.start_btn.config(text='⏹  모니터 중지', bg='#5c1a1a', fg=RED)
        self.status_dot.config(text='●  모니터링 중', fg=GREEN)
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        self._log('모니터링 시작', 'ok')

    def _stop_monitor(self):
        self.monitoring = False
        self.start_btn.config(text='▶  모니터 시작', bg='#1a5c38', fg=GREEN)
        self.status_dot.config(text='●  대기 중', fg=GRAY)
        self.detect_lbl.config(text='')
        self._log('모니터링 중지')

    def _toggle_autonomous(self):
        self.autonomous_mode = self.auto_var.get()
        if self.autonomous_mode:
            self._log('자율 모드 활성화 — 토큰 소진 시 5시간 후 자동 재개', 'warn')
        else:
            self.auto_status_lbl.config(text='')
            self.rate_limit_hit = False
            self._log('자율 모드 비활성화')

    # ──────────────────── 모니터 루프 ───────────────────────────

    def _monitor_loop(self):
        check_interval = 1.0       # 초
        ocr_every_n = 1            # 매 캡처마다 OCR (픽셀 변화 시)
        tick = 0

        while self.monitoring:
            try:
                x, y, w, h = self.region
                img = ImageGrab.grab(bbox=(x, y, x + w, y + h))

                # 이미지 변화 감지 → OCR 실행 여부 결정
                changed = self._img_changed(img)
                if changed or tick % 5 == 0:
                    text = pytesseract.image_to_string(img, lang='eng').lower()
                    self._analyze(text)

                # 미리보기 5틱마다 갱신
                if tick % 5 == 0:
                    self.root.after(0, self._refresh_preview)

                self.prev_img = img
                tick += 1
            except Exception as e:
                pass  # 스크린샷/OCR 실패는 조용히 넘김

            time.sleep(check_interval)

    def _img_changed(self, img):
        if self.prev_img is None:
            return True
        try:
            diff = ImageChops.difference(self.prev_img, img)
            bbox = diff.getbbox()
            return bbox is not None
        except Exception:
            return True

    def _analyze(self, text):
        # 권한 다이얼로그 감지
        hits = sum(1 for p in DIALOG_PATTERNS if p in text)
        if hits >= 2 and not self._response_after_id:
            self.root.after(0, lambda: self.detect_lbl.config(
                text=f'⚡ 다이얼로그 감지 ({hits}개 패턴 일치)'))
            self.root.after(0, lambda: self.status_dot.config(
                text='●  다이얼로그 감지!', fg=YELLOW))
            delay_ms = int(float(self.delay_var.get() or '1') * 1000)
            self._response_after_id = self.root.after(delay_ms, self._send_yes)

        # 자율 모드: 토큰 한도 감지
        if self.autonomous_mode and not self.rate_limit_hit:
            if any(p in text for p in RATE_LIMIT_PATTERNS):
                self.rate_limit_hit = True
                self.rate_limit_at = time.time()
                self.root.after(0, lambda: self._log(
                    '⏳ 토큰 한도 도달 감지 — 5시간 후 자동 재개 시작', 'warn'))
                self._auto_timer_thread = threading.Thread(
                    target=self._autonomous_wait, daemon=True)
                self._auto_timer_thread.start()

    # ──────────────────── Yes 전송 ───────────────────────────────

    def _send_yes(self):
        self._response_after_id = None
        try:
            x, y, w, h = self.region
            cx, cy = x + w // 2, y + h // 2

            # 1) 클릭으로 터미널 포커스
            script_click = f'''
tell application "System Events"
    set pos to {{{cx}, {cy}}}
    click at pos
end tell
'''
            subprocess.run(['osascript', '-e', script_click],
                           capture_output=True, timeout=3)
            time.sleep(0.25)

            # 2) "1" + Enter 전송
            script_key = '''
tell application "System Events"
    keystroke "1"
    delay 0.15
    key code 36
end tell
'''
            subprocess.run(['osascript', '-e', script_key],
                           capture_output=True, timeout=3)

            self._log('✅ "1" + Enter 전송 완료', 'ok')
            self.root.after(0, lambda: self.detect_lbl.config(text='✅ 승인 완료'))
            self.root.after(0, lambda: self.status_dot.config(
                text='●  모니터링 중', fg=GREEN))
            time.sleep(2)
            self.root.after(0, lambda: self.detect_lbl.config(text=''))

        except Exception as e:
            self._log(f'키 전송 오류: {e}', 'error')

    # ──────────────────── 자율 모드: 5시간 대기 ─────────────────

    def _autonomous_wait(self):
        wait_secs = 5 * 3600  # 5시간 = 18000초

        while wait_secs > 0 and self.autonomous_mode:
            h = wait_secs // 3600
            m = (wait_secs % 3600) // 60
            s = wait_secs % 60
            txt = f'⏳ 토큰 초기화까지: {h:02d}:{m:02d}:{s:02d}'
            self.root.after(0, lambda t=txt: self.auto_status_lbl.config(text=t))
            time.sleep(1)
            wait_secs -= 1

        if not self.autonomous_mode:
            return

        self._log('🚀 토큰 초기화 완료 — 재개 명령어 전송 중...', 'ok')
        self.root.after(0, lambda: self.auto_status_lbl.config(text='🚀 재개 중...'))
        time.sleep(3)
        self._send_resume()
        self.rate_limit_hit = False
        self.root.after(0, lambda: self.auto_status_lbl.config(text=''))

    def _send_resume(self):
        cmd = self.resume_var.get().strip() or '개발 계속해줘'
        try:
            # cmd에 특수문자 이스케이프
            safe_cmd = cmd.replace('"', '\\"').replace("'", "\\'")
            script = f'''
tell application "System Events"
    keystroke "{safe_cmd}"
    delay 0.3
    key code 36
end tell
'''
            subprocess.run(['osascript', '-e', script],
                           capture_output=True, timeout=5)
            self._log(f'📨 재개 명령어 전송: {cmd}', 'ok')
        except Exception as e:
            self._log(f'재개 명령어 전송 오류: {e}', 'error')

    # ──────────────────── 종료 ───────────────────────────────────

    def _on_close(self):
        self.monitoring = False
        self.autonomous_mode = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    app = ClaudeAutoApprove()
    app.run()
