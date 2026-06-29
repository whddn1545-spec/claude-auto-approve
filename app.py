#!/usr/bin/env python3
"""
Claude Auto-Approve v1.4
- [C1 fix] AppleScript 파싱: 고유 구분자(<<<FIELD>>>/<<<REC>>>) 사용
- [C4 fix] HTTP POST 핸들러 state_lock 추가
- [M1 fix] continuation 오탐 방지: 마지막 8줄만 검사
- [M2 fix] 다이얼로그 재전송 루프 방지: 세션별 활성 상태 추적
- [M3 fix] _handled dict GC 추가
- [M4 fix] settings 키 cont_cmd/continuation_cmd 통일
- [M5 fix] 토큰 재개 후 grace period 120초
- [M7 fix] 세션 배치 처리 (per-session sleep 제거)
- [v1.3] session limit 감지 + 리셋 시간 파싱 자동 재개
- [v1.3] 30분 멈춤 감지 → 재개 명령 전송 + git 자동 커밋
- [v1.3] git repo 경로 BFS 디코딩 (하이픈 포함 경로 정확 탐지)
- [v1.3] iTerm2 session_config 적용 + rate-limit 트리거 세션에만 재개 전송
- [v1.3] is_rate_limit 2개 이상 패턴 필요 (오탐 방어)
- [v1.3] cnt 쿨다운 게이트 + stall 전송 실패 복구 + dead session GC
- [v1.3] do_POST JSON 파싱 예외처리 + status 직렬화 락
"""

import json
import os
import signal
import socket
import threading
import subprocess
import time
import base64
import io
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime
from PIL import Image, ImageGrab, ImageChops

# cmux 프로세스 트리 안에 머물기 위해 nohup 없이 실행 가능하도록 SIGHUP 무시
signal.signal(signal.SIGHUP, signal.SIG_IGN)

PORT = 17654
HOST = '0.0.0.0'  # 로컬 네트워크 전체에서 접근 가능

# ─── Slack 설정 ──────────────────────────────────────────────────────
# 환경변수 또는 아래 직접 입력: export SLACK_WEBHOOK_URL="https://hooks.slack.com/..."
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
_slack_lock = threading.Lock()
_approval_last_notified = 0  # 마지막 슬랙 알림 시점의 승인 횟수


def slack_notify(text: str, emoji: str = '🤖', blocks: list = None):
    """Slack incoming webhook으로 알림 전송. SLACK_WEBHOOK_URL 미설정 시 무시."""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        import urllib.request
        payload: dict = {'text': f'{emoji} *Claude Auto-Approve*\n{text}'}
        if blocks:
            payload['blocks'] = blocks
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL, data=data,
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log(f'[Slack] 알림 실패: {e}', 'warn')


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'localhost'

# ─── 미리보기 캐시 (Fix: screencapture 과호출 방지) ─────────────────
_preview_cache: dict = {'b64': None, 'ts': 0.0}
_preview_lock = threading.Lock()
PREVIEW_MIN_INTERVAL = 5  # 최소 5초 간격, rate-limit 중엔 30초

# ─── 전역 상태 ──────────────────────────────────────────────────────
SETTINGS_FILE = os.path.expanduser('~/claude-auto-approve/settings.json')

def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_settings():
    try:
        with state_lock:
            data = {k: state[k] for k in (
                'region', 'autonomous_mode', 'continuation_mode',
                'resume_cmd', 'continuation_cmd', 'delay_sec',
                'stall_git', 'idle_send_timeout', 'session_config',
            )}
            data['autostart'] = state.get('monitoring', False)
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        pass  # 저장 실패 무시

_saved = _load_settings()

state = {
    'region': _saved.get('region', None),
    'monitoring': False,
    'autonomous_mode': _saved.get('autonomous_mode', False),
    'continuation_mode': _saved.get('continuation_mode', True),
    'rate_limit_hit': False,
    'rate_limit_remaining': 0,
    'logs': [],
    'approve_count': 0,
    'continuation_count': 0,
    'resume_cmd': _saved.get('resume_cmd', '개발 계속해줘'),
    'continuation_cmd': _saved.get('continuation_cmd', '이어서 진행해줘'),
    'delay_sec': _saved.get('delay_sec', 1.0),
    'last_status': 'idle',
    'active_sessions': 0,
    'session_config': _saved.get('session_config', {}),
    'stall_count': 0,
    'stall_git': _saved.get('stall_git', True),
    'idle_send_timeout': _saved.get('idle_send_timeout', 10 * 60),
}
state_lock = threading.Lock()


# ─── 패턴 ────────────────────────────────────────────────────────────
DIALOG_PATTERNS = [
    'do you want to proceed',
    '1. yes',
    '1: yes',
    '❯ 1',
    ') 1.',
    'esc to cancel',
    'tab to amend',
    'bash command',
    'allow this',
    'ctrl+e to explain',
    'needs input',
    'run shell command',
    'run command',
    'write to',
    'create file',
    'yes, and',
]

CONTINUATION_PATTERNS = [
    '이어서 진행할까요',
    '계속 진행할까요',
    '다음 단계로 진행할까요',
    '계속하시겠습니까',
    '다음으로 넘어갈까요',
    '이어서 진행해드릴까요',
    '계속해드릴까요',
    '진행할까요',
    'shall i continue',
    'shall i proceed',
    'would you like me to continue',
    'should i continue',
    'continue with the next',
    'proceed to the next',
]

RATE_LIMIT_PATTERNS = [
    "you've hit",
    'session limit',
    'rate limit',
    'usage limit',
    'claude is at capacity',
    'too many requests',
    'quota exceeded',
    'hour limit',
    'resets ',
    '시간 후에',
]

# 픽셀 폴백 전용 쿨다운 (dialog/continuation은 active-state 추적으로 대체)
_handled: dict[str, float] = {}
_handled_lock = threading.Lock()
HANDLE_COOLDOWN = 8.0

# 세션별 활성 상태 추적 (M2: 재전송 루프 방지)
_active_dialogs: set = set()        # 현재 다이얼로그 표시 중인 세션 id
_active_continuations: set = set()  # 현재 continuation 표시 중인 세션 id
_active_lock = threading.Lock()

# 프롬프트 idle 감지용 쿨다운 (같은 세션에 반복 전송 방지)
_cont_last_sent: dict[str, float] = {}
CONT_IDLE_COOLDOWN = 90.0  # 이어서 전송 후 90초간 재전송 억제

# 다이얼로그 승인 쿨다운 (연속 다이얼로그 이중 클릭 방지)
_dialog_last_sent: dict[str, float] = {}
DIALOG_COOLDOWN = 4.0  # 승인 후 4초간 재승인 억제


# 자율모드 재개 후 rate-limit 재감지 억제 (M5)
_resume_grace_until: float = 0.0

# 30분 멈춤 감지: 터미널 내용이 변하지 않으면 stall로 판단
_content_hash: dict[str, str] = {}
_content_unchanged_since: dict[str, float] = {}
_stall_sent: set = set()
STALL_TIMEOUT = 30 * 60       # 30분
IDLE_SEND_TIMEOUT = 10 * 60   # idle 프롬프트 전송 기준: 10분 동안 변화 없을 때


# ─── 로그 ─────────────────────────────────────────────────────────────
def log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    with state_lock:
        state['logs'].append({'ts': ts, 'msg': msg, 'level': level})
        if len(state['logs']) > 300:
            state['logs'] = state['logs'][-300:]
    print(f'[{ts}] {msg}')


# ─── AppleScript 헬퍼 ─────────────────────────────────────────────────
def osascript(script, timeout=6):
    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode == 0
    except Exception as e:
        return str(e), False


# ─── iTerm2 멀티세션 ────────────────────────────────────────────────────
_FSEP = '<<<FIELD>>>'  # 세션ID / 내용 구분자
_RSEP = '<<<REC>>>'    # 세션 레코드 구분자 (터미널 내용에 절대 등장 안 하는 토큰)

def get_iterm2_sessions():
    """모든 iTerm2 세션의 (session_id, content) 목록 반환.
    C1 fix: 리스트 대신 문자열 concatenation으로 반환 → ', ' split 파싱 버그 제거.
    """
    script = f'''
set out to ""
tell application "iTerm2"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                set sid to unique id of s
                set sc to contents of s
                set out to out & sid & "{_FSEP}" & sc & "{_RSEP}"
            end repeat
        end repeat
    end repeat
end tell
return out
'''
    text, ok = osascript(script, timeout=10)
    if not ok or not text:
        return []

    sessions = []
    for chunk in text.split(_RSEP):
        if _FSEP in chunk:
            sid, content = chunk.split(_FSEP, 1)
            sessions.append((sid.strip(), content))
    return sessions


def write_iterm2_session(session_id, text):
    """특정 iTerm2 세션에 텍스트 + Enter 전송"""
    safe = text.replace('\\', '\\\\').replace('"', '\\"')
    script = f'''
tell application "iTerm2"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if unique id of s is "{session_id}" then
                    write text "{safe}" to s
                    return "ok"
                end if
            end repeat
        end repeat
    end repeat
end tell
'''
    _, ok = osascript(script, timeout=6)
    return ok


# ─── cmux 직접 소켓 통신 (JSON-RPC) ──────────────────────────────────
import uuid as _uuid

CMUX_SOCK = os.path.expanduser('~/Library/Application Support/cmux/cmux.sock')

_cmux_inside = bool(os.environ.get('CMUX_SURFACE_ID'))  # cmux 내부에서 실행 중이면 True
_cmux_access_denied = False  # 한 번 거부되면 이후 시도 생략

def _cmux_rpc(method: str, params: dict, timeout: float = 6.0):
    """cmux Unix socket에 JSON-RPC 요청 → (result_dict, True) 또는 (err_str, False)"""
    global _cmux_access_denied
    if _cmux_access_denied:
        return 'cmux 접근 불가 (백그라운드 프로세스)', False
    import socket as _socket
    req = json.dumps({'id': str(_uuid.uuid4()), 'method': method, 'params': params}) + '\n'
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(CMUX_SOCK)
        s.sendall(req.encode())
        buf = b''
        while b'\n' not in buf:
            try:
                chunk = s.recv(65536)
            except _socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        s.close()
        if not buf.strip():
            return '소켓 응답 없음', False
        line = next((l for l in buf.split(b'\n') if l.strip()), b'')
        if not line:
            return '소켓 응답 파싱 실패 (빈 응답)', False
        try:
            resp = json.loads(line)
        except Exception:
            raw = line.decode(errors='replace')
            if 'Access denied' in raw or 'access denied' in raw.lower():
                _cmux_access_denied = True
                log('[cmux] 접근 거부 — cmux 터미널 내에서 직접 실행해야 cmux 세션 감지 가능. iTerm2 + JSONL 모드로 계속 동작합니다.', 'warn')
                return 'cmux 접근 거부', False
            return raw[:120], False
        if resp.get('ok'):
            return resp.get('result', {}), True
        return str(resp), False
    except Exception as e:
        return str(e), False

_SPINNER = set('✳⠁⠂⠃⠄⠅⠆⠇⠈⠉⠊⠋⠌⠍⠎⠏⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟⠠⠡⠢⠣⠤⠥⠦⠧⠨⠩⠪⠫⠬⠭⠮⠯⠰⠱⠲⠳⠴⠵⠶⠷⠸⠹⠺⠻⠼⠽⠾⠿⡿⢿⣻⣯⣷⣾⣽⣟⣿⣷')

def _is_claude_title(title: str) -> bool:
    """Claude Code 세션 제목인지 판별 (스피너 문자로 시작)"""
    return bool(title) and title[0] in _SPINNER

def get_cmux_surfaces():
    """cmux tree에서 모든 terminal surface 정보 반환 → [{'ref','title','type'}]"""
    result, ok = _cmux_rpc('system.tree', {'all_windows': True})
    if not ok:
        if not _cmux_access_denied:
            log(f'[cmux] tree 실패: {str(result)[:80]}', 'warn')
        return []
    surfaces = []
    for win in result.get('windows', []):
        for ws in win.get('workspaces', []):
            for pane in ws.get('panes', []):
                for surf in pane.get('surfaces', []):
                    if surf.get('type') == 'terminal':
                        surfaces.append({'ref': surf['ref'], 'title': surf.get('title', ''), 'type': 'terminal'})
    return surfaces

def _apply_session_defaults(surfaces):
    """신규 세션에 스마트 기본값 적용 (Claude 스피너 → True, 일반 shell → False)"""
    with state_lock:
        cfg = state['session_config']
        changed = False
        for s in surfaces:
            sid = f'cmux:{s["ref"]}'
            if sid not in cfg:
                is_claude = _is_claude_title(s['title'])
                cfg[sid] = {'approve': is_claude, 'continuation': is_claude, 'title': s['title']}
                changed = True
            else:
                # 제목 업데이트
                cfg[sid]['title'] = s['title']
        return changed

def get_cmux_sessions():
    """cmux read_text로 활성화된 터미널 세션 내용 읽기"""
    surfaces = get_cmux_surfaces()
    if not surfaces:
        return []
    _apply_session_defaults(surfaces)
    results = []
    for s in surfaces:
        sid = f'cmux:{s["ref"]}'
        with state_lock:
            cfg = state['session_config'].get(sid, {})
        if not cfg.get('approve') and not cfg.get('continuation'):
            continue  # 둘 다 비활성이면 읽지 않음
        result, ok = _cmux_rpc('surface.read_text', {'surface_id': s['ref'], 'lines': 60, 'scrollback': True})
        if ok:
            raw = base64.b64decode(result.get('base64', '')).decode('utf-8', errors='replace')
            if raw.strip():
                results.append((sid, raw))
    return results

def write_cmux_session(sid, text):
    """cmux send_text로 터미널에 텍스트 + Enter 전송"""
    surf = sid.replace('cmux:', '')
    _, ok = _cmux_rpc('surface.send_text', {'surface_id': surf, 'text': text + '\r'})
    return ok


# ─── 감지 로직 ────────────────────────────────────────────────────────
def is_dialog(text: str) -> bool:
    # 스크롤백의 이전 다이얼로그 오탐 방지: 마지막 15줄만 검사
    lines = text.strip().splitlines()
    t = '\n'.join(lines[-15:]).lower()
    hits = sum(1 for p in DIALOG_PATTERNS if p in t)
    return hits >= 2


def is_continuation(text: str) -> bool:
    # M1 fix: 전체 텍스트 대신 마지막 8줄만 검사 → 오탐 대폭 감소
    lines = text.strip().splitlines()
    recent = '\n'.join(lines[-8:]).lower()
    return any(p in recent for p in CONTINUATION_PATTERNS)


def is_prompt_idle(text: str) -> bool:
    """Claude Code 입력 대기 프롬프트 감지 — 완료 후 대기 중인 경우"""
    lines = text.strip().splitlines()
    tail = '\n'.join(lines[-8:]).lower()
    # '? for shortcuts' 는 Claude 입력 대기 중에만 표시됨 → 가장 신뢰할 수 있는 idle 지표
    if '? for shortcuts' in tail:
        return True
    # 폴백: ❯ 단독 라인 (다이얼로그 등 오탐 방지용 기존 체크)
    for line in lines[-6:]:
        stripped = line.strip()
        if stripped.startswith('❯') and len(stripped.split()) <= 1:
            return True
    return False


def is_rate_limit(text: str) -> bool:
    t = text.lower()
    # 오탐 방지: 2개 이상 패턴 매칭 필요 (단일 광범위 패턴 오탐 방어)
    return sum(1 for p in RATE_LIMIT_PATTERNS if p in t) >= 2


def parse_reset_epoch(text: str) -> float:
    """'resets 1am' / 'resets 7:20pm (Asia/Seoul)' 에서 리셋 epoch 추출. 실패 시 0."""
    import re
    from datetime import datetime, timedelta
    # 분(minute)을 선택적으로 처리 — "resets 1am"(분 없음)도 매칭
    m = re.search(r'resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text, re.IGNORECASE)
    if not m:
        return 0.0
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or '').lower()
    if ampm == 'pm' and hour != 12:
        hour += 12
    elif ampm == 'am' and hour == 12:
        hour = 0
    now = datetime.now()
    reset = now.replace(hour=hour, minute=minute, second=5, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset.timestamp()


# ─── Git 자동 커밋 ────────────────────────────────────────────────────
def _resolve_project_path(encoded: str) -> str:
    """BFS로 Claude 프로젝트 디렉터리명 → 실제 경로 복원.
    '-Users-foo-my-app' 처럼 경로명에 하이픈이 포함된 경우도 처리.
    """
    from collections import deque
    if not encoded.startswith('-'):
        return ''
    tokens = encoded[1:].split('-')
    if not tokens or not tokens[0]:
        return ''
    queue = deque([('/' + tokens[0], 1)])
    found = []
    while queue:
        path, idx = queue.popleft()
        if idx == len(tokens):
            if os.path.isdir(path):
                found.append(path)
            continue
        tok = tokens[idx]
        # Option A: '-' = 경로 구분자 (현재 경로가 실제로 존재할 때만 분기)
        if os.path.isdir(path):
            queue.append((path + '/' + tok, idx + 1))
        # Option B: '-' = 이름의 일부 (항상 시도)
        queue.append((path + '-' + tok, idx + 1))
    git_repos = [p for p in found if os.path.exists(p + '/.git')]
    return git_repos[0] if git_repos else (found[0] if found else '')


def _find_active_git_repos(max_age_hours: int = 8) -> list:
    """최근 Claude가 작업한 git 레포지토리 경로 목록 반환."""
    projects_dir = os.path.expanduser('~/.claude/projects')
    if not os.path.isdir(projects_dir):
        return []
    cutoff = time.time() - max_age_hours * 3600
    repos = []
    for name in sorted(os.listdir(projects_dir)):
        dir_path = os.path.join(projects_dir, name)
        if not os.path.isdir(dir_path):
            continue
        try:
            jsonl_files = [f for f in os.listdir(dir_path) if f.endswith('.jsonl')]
        except OSError:
            continue
        if not any(os.path.getmtime(os.path.join(dir_path, f)) > cutoff for f in jsonl_files):
            continue
        resolved = _resolve_project_path(name)
        if resolved and os.path.exists(resolved + '/.git') and resolved not in repos:
            repos.append(resolved)
    return repos


def _get_work_summary(repo_path: str, max_age_hours: int = 8) -> str:
    """가장 최근 Claude 세션의 마지막 assistant 메시지 요약 반환."""
    projects_dir = os.path.expanduser('~/.claude/projects')
    encoded = repo_path.replace('/', '-')  # '/Users/foo' → '-Users-foo'
    session_dir = os.path.join(projects_dir, encoded)
    if not os.path.isdir(session_dir):
        return ''
    cutoff = time.time() - max_age_hours * 3600
    recent = sorted(
        [f for f in os.listdir(session_dir) if f.endswith('.jsonl')
         and os.path.getmtime(os.path.join(session_dir, f)) > cutoff],
        key=lambda f: os.path.getmtime(os.path.join(session_dir, f)),
        reverse=True
    )
    if not recent:
        return ''
    try:
        lines = open(os.path.join(session_dir, recent[0])).readlines()
        for line in reversed(lines):
            d = json.loads(line)
            msg = d.get('message', d)
            if msg.get('role') == 'assistant':
                content = msg.get('content', '')
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get('type') == 'text':
                            return c.get('text', '')[:200].replace('\n', ' ')
                elif isinstance(content, str):
                    return content[:200].replace('\n', ' ')
    except Exception:
        pass
    return ''


def auto_git_commit_push(repo_path: str):
    """변경사항 add → commit → push. 변경 없으면 스킵."""
    try:
        r = subprocess.run(['git', '-C', repo_path, 'status', '--porcelain'],
                           capture_output=True, text=True, timeout=10)
        if not r.stdout.strip():
            log(f'[git] {repo_path}: 변경사항 없음', 'info')
            return
        summary = _get_work_summary(repo_path) or '자동 저장'
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        commit_msg = f'[Auto] {ts} — {summary[:120]}'
        subprocess.run(['git', '-C', repo_path, 'add', '-A'], capture_output=True, timeout=10)
        r = subprocess.run(['git', '-C', repo_path, 'commit', '-m', commit_msg],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            log(f'[git] 커밋 실패 ({repo_path}): {r.stderr.strip()[:80]}', 'error')
            return
        log(f'[git] 커밋 완료: {repo_path}', 'ok')
        r = subprocess.run(['git', '-C', repo_path, 'push'],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            log(f'[git] 푸시 완료: {repo_path}', 'ok')
        else:
            log(f'[git] 푸시 실패: {r.stderr.strip()[:80]}', 'error')
    except Exception as e:
        log(f'[git] 오류 ({repo_path}): {e}', 'error')


def can_handle(session_id: str) -> bool:
    """픽셀 폴백 전용 쿨다운. M3 fix: 10분 이상 된 항목 GC."""
    now = time.time()
    with _handled_lock:
        stale = [k for k, v in _handled.items() if now - v > 600]
        for k in stale:
            del _handled[k]
        if now - _handled.get(session_id, 0) < HANDLE_COOLDOWN:
            return False
        _handled[session_id] = now
        return True


# ─── 픽셀 기반 다이얼로그 위치 찾기 ────────────────────────────────────
def find_dialog_locations(img: Image.Image):
    """
    Claude Code 다이얼로그의 청록색 ")" 선택 표시 픽셀을 찾아
    화면상 중심 좌표 목록 반환 (region 기준 상대 좌표)
    """
    rgb = img.convert('RGB')
    w, h = rgb.size
    clusters = []
    last_x = -100

    # x방향으로 스캔 — 청록 픽셀 열이 10px 이상 있으면 다이얼로그
    for x in range(0, w, 3):
        col_cyan = 0
        for y in range(0, h, 3):
            r, g, b = rgb.getpixel((x, y))
            if r < 80 and g > 140 and b > 190:
                col_cyan += 1
        if col_cyan >= 3 and x - last_x > 50:
            clusters.append(x)
            last_x = x

    return clusters  # 각 x좌표가 하나의 다이얼로그 열


# ─── 메인 모니터 루프 ──────────────────────────────────────────────────
def monitor_loop():
    global _active_dialogs, _active_continuations, _resume_grace_until
    global _content_hash, _content_unchanged_since, _stall_sent
    import hashlib
    prev_img = None
    tick = 0
    log('모니터링 루프 시작', 'ok')

    while state['monitoring']:
        try:
            # Rate-limit 중엔 세션 읽기 스킵하고 60초 대기 (Fix: WindowServer 과부하 방지)
            with state_lock:
                is_rate_limited = state['rate_limit_hit']
            if is_rate_limited:
                time.sleep(60)
                tick += 1
                continue

            # ── 1) cmux + iTerm2 세션 스캔 ──────────────────────────
            iterm_sessions = get_iterm2_sessions()
            # Fix 2: iTerm2 세션에도 session_config 기본값 적용
            with state_lock:
                for sid, _ in iterm_sessions:
                    if sid not in state['session_config']:
                        state['session_config'][sid] = {'approve': True, 'continuation': True, 'title': sid}
            sessions = get_cmux_sessions() + iterm_sessions
            with state_lock:
                state['active_sessions'] = len(sessions)

            # 디버그: 첫 3틱 동안 + 이후 30틱마다 세션 내용 확인
            if tick < 3 or tick % 30 == 0:
                if sessions:
                    for sid, content in sessions:
                        preview = content.strip()[-300:].replace('\n', '↵')
                        log(f'[DBG] {sid[:20]}: {preview}', 'info')
                else:
                    log('[DBG] 세션 없음 (iTerm2/cmux 미감지)', 'warn')

            current_sids = {sid for sid, _ in sessions}

            # M7 fix: 배치 수집 후 한 번에 처리 (per-session sleep 제거)
            to_approve  = []  # (sid,) — 새로 다이얼로그 발생
            to_continue = []  # (sid,) — 새로 continuation 발생
            to_stall    = []  # (sid,) — 30분 이상 멈춤
            rate_triggered = False
            rate_limit_content = ''
            rate_limit_sid = ''

            for sid, content in sessions:
                # 세션별 설정 확인
                with state_lock:
                    scfg = state['session_config'].get(sid, {'approve': True, 'continuation': True})
                approve_on = scfg.get('approve', True)
                cont_on = scfg.get('continuation', True)

                # 콘텐츠 변경 감지 (stall 판단용 — 마지막 1500자만 해시)
                cur_hash = hashlib.md5(content[-1500:].encode()).hexdigest()
                if _content_hash.get(sid) != cur_hash:
                    _content_hash[sid] = cur_hash
                    _content_unchanged_since[sid] = time.time()
                    _stall_sent.discard(sid)  # 내용 바뀌면 stall 상태 초기화

                # Fix 6: 루프마다 락 안에서 플래그 스냅샷
                with state_lock:
                    cont_mode_on = state['continuation_mode']

                dlg = approve_on and is_dialog(content)
                cont_mode = cont_mode_on and cont_on
                # Fix 4: cnt도 쿨다운 게이트 적용
                now = time.time()
                cnt = (cont_mode and is_continuation(content)
                       and (now - _cont_last_sent.get(sid, 0)) >= CONT_IDLE_COOLDOWN)
                # idle: ❯ 프롬프트 상태 + 콘텐츠가 IDLE_SEND_TIMEOUT(기본 10분) 동안 안 변했을 때만
                with state_lock:
                    idle_timeout = state.get('idle_send_timeout', IDLE_SEND_TIMEOUT)
                idle = (cont_mode and not cnt and not dlg
                        and is_prompt_idle(content)
                        and (now - _content_unchanged_since.get(sid, now)) >= idle_timeout
                        and (now - _cont_last_sent.get(sid, 0)) >= CONT_IDLE_COOLDOWN)

                # 30분 이상 멈춤 감지 (idle 프롬프트 상태 + 내용 unchanged)
                stall = (
                    is_prompt_idle(content)
                    and sid not in _stall_sent
                    and (time.time() - _content_unchanged_since.get(sid, time.time())) > STALL_TIMEOUT
                )

                with _active_lock:
                    if not dlg and sid in _active_dialogs:
                        _active_dialogs.discard(sid)
                    if not (cnt or idle) and sid in _active_continuations:
                        _active_continuations.discard(sid)

                    # 쿨다운 지났으면 _active_dialogs에서도 클리어 → 새 다이얼로그 감지 허용
                    if dlg and sid in _active_dialogs:
                        if (now - _dialog_last_sent.get(sid, 0)) >= DIALOG_COOLDOWN:
                            _active_dialogs.discard(sid)

                    if dlg and sid not in _active_dialogs:
                        _active_dialogs.add(sid)
                        to_approve.append(sid)

                    if (cnt or idle) and sid not in _active_continuations:
                        _active_continuations.add(sid)
                        to_continue.append(sid)
                        if idle:
                            log(f'[idle] {sid[:20]}: Claude 대기 감지 → 이어서 진행 전송', 'info')

                if stall:
                    to_stall.append(sid)
                    _stall_sent.add(sid)

                # M5 fix: grace period 적용
                with state_lock:
                    _auto = state['autonomous_mode']
                    _hit  = state['rate_limit_hit']
                if (_auto and not _hit
                        and time.time() > _resume_grace_until
                        and is_rate_limit(content)):
                    rate_triggered = True
                    rate_limit_content = content
                    rate_limit_sid = sid

            # 배치 처리: delay는 한 번만
            if to_approve or to_continue:
                with state_lock:
                    state['last_status'] = 'detected'
                delay = state.get('delay_sec', 1.0)
                if delay > 0:
                    time.sleep(delay)

                for sid in to_approve:
                    if sid.startswith('cmux:'):
                        ok = write_cmux_session(sid, '1')
                    else:
                        ok = write_iterm2_session(sid, '1')
                    if ok:
                        _dialog_last_sent[sid] = time.time()
                        with _active_lock:
                            _active_dialogs.discard(sid)  # 승인 즉시 클리어 → 다음 다이얼로그 바로 감지
                        with state_lock:
                            state['approve_count'] += 1
                            cnt_now = state['approve_count']
                        label = sid.split(':')[0]
                        log(f'✅ 승인 → {label} (총 {cnt_now}회)', 'ok')
                        # 10회마다 슬랙 알림
                        global _approval_last_notified
                        if cnt_now % 10 == 0 and cnt_now != _approval_last_notified:
                            _approval_last_notified = cnt_now
                            slack_notify(f'✅ 권한 승인 *{cnt_now}회* 완료', '✅')
                    else:
                        log(f'세션 입력 실패: {sid}', 'error')
                        with _active_lock:
                            _active_dialogs.discard(sid)

                cmd = state.get('continuation_cmd', '이어서 진행해줘')
                for sid in to_continue:
                    if sid.startswith('cmux:'):
                        ok = write_cmux_session(sid, cmd)
                    else:
                        ok = write_iterm2_session(sid, cmd)
                    if ok:
                        _cont_last_sent[sid] = time.time()
                        with state_lock:
                            state['continuation_count'] += 1
                        log(f'📨 이어서 진행 → 세션 {sid[:8]}... "{cmd}" (총 {state["continuation_count"]}회)', 'ok')

                with state_lock:
                    state['last_status'] = 'approved' if to_approve else 'monitoring'
                time.sleep(1.5)
                with state_lock:
                    state['last_status'] = 'monitoring'

            # 30분 stall 처리
            if to_stall:
                with state_lock:
                    resume_cmd = state.get('resume_cmd', '개발 계속해줘')
                    do_git = state.get('stall_git', True)
                for sid in to_stall:
                    ok = (write_cmux_session(sid, resume_cmd) if sid.startswith('cmux:')
                          else write_iterm2_session(sid, resume_cmd))
                    if ok:
                        with state_lock:
                            state['stall_count'] += 1
                            sc = state['stall_count']
                        log(f'⏰ 30분 멈춤 감지 → "{resume_cmd}" 전송 (총 {sc}회)', 'warn')
                        ip = _local_ip()
                        slack_notify(
                            f'⏰ *30분 멈춤 감지* — 재개 명령 전송 (총 {sc}회)\n'
                            f'명령: `{resume_cmd}`\n'
                            f'모바일 제어: http://{ip}:{PORT}/m',
                            '⏰')
                    else:
                        # Fix 5: 전송 실패 시 stall_sent 해제 → 일정 시간 후 재시도 허용
                        _stall_sent.discard(sid)
                        log(f'⏰ stall 전송 실패: {sid[:20]} — 재시도 허용', 'warn')
                if do_git:
                    repos = _find_active_git_repos()
                    if repos:
                        for repo in repos:
                            threading.Thread(target=auto_git_commit_push, args=(repo,), daemon=True).start()
                    else:
                        log('[git] 활성 git 레포지토리를 찾을 수 없음', 'warn')

            # 토큰 한도
            if rate_triggered:
                reset_epoch = parse_reset_epoch(rate_limit_content)
                wait_sec = max(60, int(reset_epoch - time.time())) if reset_epoch else 5 * 3600
                with state_lock:
                    state['rate_limit_hit'] = True
                    state['rate_limit_remaining'] = wait_sec
                if reset_epoch:
                    from datetime import datetime
                    reset_str = datetime.fromtimestamp(reset_epoch).strftime('%H:%M:%S')
                    log(f'⏳ 세션 한도 감지 — {reset_str}에 자동 재개', 'warn')
                    slack_notify(f'⏳ *토큰 한도 도달* — `{reset_str}`에 자동 재개\n모바일 제어: http://{_local_ip()}:{PORT}/m', '⏳')
                else:
                    log('⏳ 세션 한도 감지 — 5시간 후 자동 재개 (리셋 시간 파싱 실패)', 'warn')
                    slack_notify(f'⏳ *토큰 한도 도달* — 5시간 후 자동 재개\n모바일 제어: http://{_local_ip()}:{PORT}/m', '⏳')
                threading.Thread(target=auto_wait_loop, args=(reset_epoch, rate_limit_sid), daemon=True).start()

            # 종료된 세션 정리 (Fix 10: 보조 dict도 GC)
            with _active_lock:
                _active_dialogs      &= current_sids
                _active_continuations &= current_sids
            dead = set(_content_hash) - current_sids
            for d in dead:
                _content_hash.pop(d, None)
                _content_unchanged_since.pop(d, None)
                _cont_last_sent.pop(d, None)
                _dialog_last_sent.pop(d, None)
                _stall_sent.discard(d)

            tick += 1

        except Exception as e:
            log(f'모니터 루프 오류: {e}', 'error')

        time.sleep(2)  # 2초 간격 — osascript 호출·메모리 점유 절반으로 감소

    log('모니터링 루프 종료')


# ─── 자율 모드 타이머 ──────────────────────────────────────────────────
def auto_wait_loop(reset_epoch: float = 0, trigger_sid: str = ''):
    global _resume_grace_until

    target = reset_epoch if reset_epoch > time.time() else time.time() + 5 * 3600
    while time.time() < target:
        with state_lock:
            if not state['autonomous_mode']:
                return
            state['rate_limit_remaining'] = max(0, int(target - time.time()))
        time.sleep(5)  # 1초→5초: UI countdown 정밀도 소폭 감소, CPU 낭비 제거

    with state_lock:
        state['rate_limit_remaining'] = 0
        still_on = state['autonomous_mode'] and state['rate_limit_hit']
    if not still_on:
        return

    log('🚀 토큰 초기화 완료 — 재개 명령어 전송', 'ok')
    slack_notify('🚀 *토큰 초기화 완료* — 작업 재개 중', '🚀')
    time.sleep(3)

    with state_lock:
        cmd = state.get('resume_cmd', '개발 계속해줘')
        scfgs = dict(state['session_config'])

    sent = 0
    # Fix 3: 트리거한 세션에만 전송 (+ config 확인)
    if trigger_sid:
        cfg = scfgs.get(trigger_sid, {})
        if cfg.get('continuation', True):
            ok = (write_cmux_session(trigger_sid, cmd) if trigger_sid.startswith('cmux:')
                  else write_iterm2_session(trigger_sid, cmd))
            if ok:
                sent += 1

    # 트리거 세션 전송 실패 시 continuation 켜진 모든 세션으로 폴백
    if not sent:
        for s in get_cmux_surfaces():
            sid = f'cmux:{s["ref"]}'
            if scfgs.get(sid, {}).get('continuation', True):
                if write_cmux_session(sid, cmd):
                    sent += 1
        for sid, _ in get_iterm2_sessions():
            if scfgs.get(sid, {}).get('continuation', True):  # Fix 2: config 확인
                if write_iterm2_session(sid, cmd):
                    sent += 1

    if sent:
        log(f'📨 재개 명령어 전송 → {sent}개 세션', 'ok')
    else:
        safe = cmd.replace('"', '\\"')
        osascript(f'''
tell application "System Events"
    keystroke "{safe}"
    delay 0.3
    key code 36
end tell
''')
        log('📨 재개 명령어 전송 (폴백)', 'ok')

    with state_lock:
        state['rate_limit_hit'] = False

    # M5 fix: 재개 후 120초 동안 rate-limit 재감지 억제
    _resume_grace_until = time.time() + 120
    log('⏸ grace period 2분 — rate-limit 재감지 억제 중', 'info')


# ─── 화면 크기 조회 (Screen Recording 권한 불필요) ────────────────────────
def get_screen_size():
    """osascript로 화면 해상도 반환 — Screen Recording 권한 불필요"""
    out, ok = osascript('tell application "Finder" to get bounds of window of desktop')
    if ok and out:
        parts = [p.strip() for p in out.split(',')]
        if len(parts) == 4:
            try:
                return int(parts[2]), int(parts[3])
            except ValueError:
                pass
    # 폴백: NSScreen via system_profiler
    try:
        r = subprocess.run(['system_profiler', 'SPDisplaysDataType'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if 'Resolution' in line:
                nums = [int(x) for x in line.split() if x.isdigit()]
                if len(nums) >= 2:
                    return nums[0], nums[1]
    except Exception:
        pass
    return 1440, 900


# ─── 미리보기 캡처 (Screen Recording 없이 영역만 반환) ──────────────────────
def _grab_screen(bbox=None):
    """screencapture CLI → PIL Image. 실패 시 None."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        tmp = f.name
    try:
        if bbox:
            x, y, w, h = bbox
            cmd = ['screencapture', '-x', '-R', f'{x},{y},{w},{h}', tmp]
        else:
            cmd = ['screencapture', '-x', tmp]
        r = subprocess.run(cmd, capture_output=True, timeout=3)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            img = Image.open(tmp)
            img.load()
            return img.copy()
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    return None


def capture_region_b64():
    region = state['region']
    if not region:
        return None
    now = time.time()
    # Rate-limit 중엔 30초, 평상시엔 5초 간격으로만 실제 캡처
    with state_lock:
        limited = state['rate_limit_hit']
    min_interval = 30 if limited else PREVIEW_MIN_INTERVAL
    with _preview_lock:
        if now - _preview_cache['ts'] < min_interval and _preview_cache['b64']:
            return _preview_cache['b64']
    try:
        x, y, w, h = region
        img = _grab_screen((x, y, w, h))
        if img is None and not limited:  # rate-limit 중엔 fallback 금지 (메모리 고갈 방지)
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        img.thumbnail((700, 350), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=72)
        b64 = base64.b64encode(buf.getvalue()).decode()
        with _preview_lock:
            _preview_cache['b64'] = b64
            _preview_cache['ts'] = now
        return b64
    except Exception:
        with _preview_lock:
            return _preview_cache['b64']


def capture_fullscreen_b64():
    sw, sh = get_screen_size()
    try:
        img = _grab_screen()
        if img is None:
            img = ImageGrab.grab()
        orig_w, orig_h = img.size
        img.thumbnail((1400, 900), Image.LANCZOS)
        disp_w, disp_h = img.size
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=65)
        return base64.b64encode(buf.getvalue()).decode(), orig_w, orig_h, disp_w, disp_h, sw, sh
    except Exception:
        return None, sw, sh, sw, sh, sw, sh



# ─── HTTP 핸들러 ────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(data))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html):
        data = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path

        if p in ('/', '/index.html'):
            self._html(HTML)

        elif p in ('/m', '/mobile'):
            self._html(MOBILE_HTML)

        elif p == '/api/status':
            # Fix 7: 락 안에서 스냅샷 후 직렬화 (logs 등 동시 쓰기 충돌 방지)
            with state_lock:
                rem = state['rate_limit_remaining']
                snap = {
                    'monitoring': state['monitoring'],
                    'autonomous': state['autonomous_mode'],
                    'continuation_mode': state['continuation_mode'],
                    'rate_limit_hit': state['rate_limit_hit'],
                    'rate_limit_countdown': f'{rem//3600:02d}:{(rem%3600)//60:02d}:{rem%60:02d}' if rem else '',
                    'approve_count': state['approve_count'],
                    'continuation_count': state['continuation_count'],
                    'stall_count': state['stall_count'],
                    'last_status': state['last_status'],
                    'region': state['region'],
                    'active_sessions': state['active_sessions'],
                    'logs': list(state['logs'][-60:]),
                    'resume_cmd': state['resume_cmd'],
                    'continuation_cmd': state['continuation_cmd'],
                    'delay_sec': state['delay_sec'],
                    'stall_git': state['stall_git'],
                    'idle_send_timeout': state['idle_send_timeout'],
                }
            self._json(200, snap)

        elif p == '/api/preview':
            self._json(200, {'image': capture_region_b64()})

        elif p == '/api/fullscreen':
            b64, ow, oh, dw, dh, sw, sh = capture_fullscreen_b64()
            self._json(200, {'image': b64, 'orig_w': ow, 'orig_h': oh,
                             'disp_w': dw, 'disp_h': dh,
                             'screen_w': sw, 'screen_h': sh})

        elif p == '/api/sessions':
            # 현재 활성 세션 목록 + 각 세션의 설정 반환
            surfaces = get_cmux_surfaces()
            iterm = get_iterm2_sessions()
            with state_lock:
                cfg = dict(state['session_config'])
            result = []
            for s in surfaces:
                sid = f'cmux:{s["ref"]}'
                scfg = cfg.get(sid, {'approve': _is_claude_title(s['title']), 'continuation': _is_claude_title(s['title'])})
                result.append({'sid': sid, 'title': s['title'] or sid, 'approve': scfg.get('approve', True), 'continuation': scfg.get('continuation', True), 'type': 'cmux'})
            for sid, _ in iterm:
                scfg = cfg.get(sid, {'approve': True, 'continuation': True})
                result.append({'sid': sid, 'title': sid, 'approve': scfg.get('approve', True), 'continuation': scfg.get('continuation', True), 'type': 'iterm2'})
            self._json(200, {'sessions': result})

        else:
            self._json(404, {'error': 'not found'})

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            self._json(400, {'error': 'invalid JSON'})
            return
        p = urlparse(self.path).path

        if p == '/api/toggle':
            # C4 fix: state_lock으로 복합 상태 일관성 보장
            with state_lock:
                if state['monitoring']:
                    state['monitoring'] = False
                    state['last_status'] = 'idle'
                    state['active_sessions'] = 0
                    do_log = ('모니터링 중지', 'info')
                    start = False
                else:
                    if not state['region']:
                        self._json(400, {'error': '영역을 먼저 선택하세요'})
                        return
                    state['monitoring'] = True
                    state['last_status'] = 'monitoring'
                    do_log = ('모니터링 시작', 'ok')
                    start = True
                monitoring = state['monitoring']
            log(*do_log)
            _save_settings()
            if start:
                threading.Thread(target=monitor_loop, daemon=True).start()
            self._json(200, {'monitoring': monitoring})

        elif p == '/api/region':
            r = body.get('region')
            if r and len(r) == 4:
                with state_lock:
                    state['region'] = r
                log(f'영역 설정: ({r[0]},{r[1]}) {r[2]}×{r[3]}px', 'ok')
                _save_settings()
                self._json(200, {'ok': True})
            else:
                self._json(400, {'error': 'region must be [x,y,w,h]'})

        elif p == '/api/autonomous':
            enabled = body.get('enabled', False)
            with state_lock:
                state['autonomous_mode'] = enabled
                if not enabled:
                    state['rate_limit_hit'] = False
                    state['rate_limit_remaining'] = 0
            log(f'자율 모드 {"활성화" if enabled else "비활성화"}')
            self._json(200, {'ok': True})

        elif p == '/api/continuation':
            enabled = body.get('enabled', True)
            with state_lock:
                state['continuation_mode'] = enabled
            log(f'이어서 진행 모드 {"활성화" if enabled else "비활성화"}')
            self._json(200, {'ok': True})

        elif p == '/api/settings':
            with state_lock:
                for k in ('resume_cmd', 'continuation_cmd'):
                    if k in body:
                        state[k] = str(body[k])
                if 'delay_sec' in body:
                    state['delay_sec'] = float(body['delay_sec'])
                if 'stall_git' in body:
                    state['stall_git'] = bool(body['stall_git'])
                if 'idle_send_timeout' in body:
                    state['idle_send_timeout'] = max(60, int(body['idle_send_timeout']))
            self._json(200, {'ok': True})

        elif p == '/api/send':
            # 모바일에서 특정 세션에 커스텀 명령 전송
            cmd = body.get('cmd', '').strip()
            sid = body.get('sid', '')
            if not cmd:
                self._json(400, {'error': 'cmd required'})
                return
            sent = 0
            if sid:
                targets = [sid]
            else:
                with state_lock:
                    targets = [s for s, c in state['session_config'].items() if c.get('continuation', True)]
            for target in targets:
                ok = (write_cmux_session(target, cmd) if target.startswith('cmux:')
                      else write_iterm2_session(target, cmd))
                if ok:
                    sent += 1
            if not targets:
                # 폴백: 모든 cmux 세션
                for s in get_cmux_surfaces():
                    if write_cmux_session(f'cmux:{s["ref"]}', cmd):
                        sent += 1
            log(f'📱 모바일 명령 전송: "{cmd}" → {sent}개 세션', 'ok')
            slack_notify(f'📱 *모바일 명령 전송*: `{cmd}`', '📱')
            self._json(200, {'ok': True, 'sent': sent})

        elif p == '/api/session-config':
            sid = body.get('sid', '')
            if not sid:
                self._json(400, {'error': 'sid required'})
                return
            with state_lock:
                cfg = state['session_config'].setdefault(sid, {'approve': True, 'continuation': True, 'title': sid})
                if 'approve' in body:
                    cfg['approve'] = bool(body['approve'])
                if 'continuation' in body:
                    cfg['continuation'] = bool(body['continuation'])
            log(f'[세션] {sid[:24]}: approve={cfg["approve"]} cont={cfg["continuation"]}', 'info')
            self._json(200, {'ok': True})

        else:
            self._json(404, {'error': 'not found'})


# ─── HTML UI ────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>Claude Auto-Approve</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#090912;color:#dde;font-family:'SF Pro Display',-apple-system,sans-serif;min-height:100vh;font-size:13px}
header{background:#10101f;padding:14px 20px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #1c1c35}
header h1{font-size:16px;font-weight:700;color:#00d4ff}
.pill{background:#1a1a35;border-radius:20px;padding:2px 10px;font-size:11px;color:#00d4ff;margin-left:6px}
.pill.green{color:#00ff88;background:#0d2a1d}
.ml-auto{margin-left:auto;display:flex;align-items:center;gap:8px}
main{display:grid;grid-template-columns:280px 1fr;gap:14px;padding:14px;height:calc(100vh - 51px)}
.sidebar{display:flex;flex-direction:column;gap:10px;overflow-y:auto}
.panel{background:#10101f;border:1px solid #1c1c35;border-radius:10px;padding:14px}
.panel h2{font-size:11px;font-weight:600;color:#00d4ff;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}

/* 상태 표시 */
.stat-row{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:#090912;border-radius:7px;margin-bottom:6px}
.stat-label{font-size:11px;color:#666}
.stat-val{font-size:13px;font-weight:700;color:#e0e0e0}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#333;margin-right:6px;vertical-align:middle}
.dot.on{background:#00ff88;box-shadow:0 0 6px #00ff8877}
.dot.warn{background:#f39c12;box-shadow:0 0 6px #f39c1277;animation:blink .7s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.dot.idle{background:#444}

/* 버튼 */
.btn{display:flex;align-items:center;justify-content:center;gap:6px;padding:9px 14px;border:none;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;width:100%;transition:.15s}
.btn-start{background:#0d3322;color:#00ff88;border:1px solid #00ff8830}
.btn-start:hover{background:#103d28}
.btn-stop{background:#33100d;color:#ff7070;border:1px solid #ff707030}
.btn-stop:hover{background:#3d1310}
.btn-region{background:#0d1833;color:#00d4ff;border:1px solid #00d4ff30;margin-bottom:6px}
.btn-region:hover{background:#101e40}

/* 토글 스위치 */
.toggle-row{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#090912;border-radius:7px;cursor:pointer;margin-bottom:6px;user-select:none}
.toggle-row:hover{background:#0d0d1e}
.sw{width:36px;height:20px;background:#222;border-radius:10px;position:relative;flex-shrink:0;transition:.2s}
.sw::after{content:'';position:absolute;width:14px;height:14px;background:#555;border-radius:50%;top:3px;left:3px;transition:.2s}
.sw.on{background:#00d4ff25;border:1px solid #00d4ff50}
.sw.on::after{background:#00d4ff;left:19px}
.sw.green.on{background:#00ff8820;border:1px solid #00ff8840}
.sw.green.on::after{background:#00ff88}
.sw-label{font-size:12px;color:#aaa;line-height:1.4}

/* 입력 */
.field{margin-bottom:8px}
.field label{display:block;font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.field input{width:100%;background:#090912;border:1px solid #1c1c35;border-radius:6px;padding:7px 9px;color:#dde;font-size:12px;outline:none;transition:.15s}
.field input:focus{border-color:#00d4ff40}

/* 오른쪽: 미리보기 + 로그 */
.right-col{display:flex;flex-direction:column;gap:10px;min-height:0}
#preview-wrap{background:#10101f;border:1px solid #1c1c35;border-radius:10px;padding:10px;flex-shrink:0}
#preview-wrap h2{font-size:11px;font-weight:600;color:#00d4ff;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
#preview{width:100%;height:220px;object-fit:contain;background:#06060e;border-radius:6px;display:block}
#preview-ph{width:100%;height:220px;background:#06060e;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#2a2a40;font-size:12px}
.region-info{font-size:10px;color:#444;margin-top:5px;text-align:center}

#log-wrap{background:#10101f;border:1px solid #1c1c35;border-radius:10px;padding:10px;flex:1;min-height:0;display:flex;flex-direction:column}
#log-wrap h2{font-size:11px;font-weight:600;color:#00d4ff;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;flex-shrink:0}
#log{background:#05050d;border-radius:6px;padding:8px;flex:1;overflow-y:auto;font-family:'Menlo',monospace;font-size:10.5px;line-height:1.55}
.l-info{color:#3a7}
.l-warn{color:#d4930a}
.l-error{color:#c0392b}
.l-ok{color:#00e87a}
.l-ts{color:#2a2a40}

/* 세션 목록 */
.sess-row{display:flex;align-items:center;gap:8px;padding:7px 10px;background:#090912;border-radius:7px;margin-bottom:5px}
.sess-title{flex:1;font-size:11px;color:#bbb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sess-type{font-size:9px;color:#444;flex-shrink:0}
.sess-toggles{display:flex;gap:6px;flex-shrink:0}
.mini-sw{width:28px;height:16px;background:#222;border-radius:8px;position:relative;cursor:pointer;flex-shrink:0}
.mini-sw::after{content:'';position:absolute;width:10px;height:10px;background:#555;border-radius:50%;top:3px;left:3px;transition:.15s}
.mini-sw.on{background:#00d4ff30;border:1px solid #00d4ff60}
.mini-sw.on::after{background:#00d4ff;left:15px}
.mini-sw.green.on{background:#00ff8820;border:1px solid #00ff8840}
.mini-sw.green.on::after{background:#00ff88}
.sess-label{font-size:9px;color:#444;text-align:center;margin-top:1px}

/* 타이머 */
.timer-box{text-align:center;padding:8px;background:#1a0f00;border-radius:7px;display:none}
.timer-box .t{font-size:26px;font-weight:700;color:#f39c12;letter-spacing:2px;font-variant-numeric:tabular-nums}
.timer-box .tl{font-size:10px;color:#6a4a00;margin-top:2px}

/* 세션 뱃지 */
.session-badge{display:inline-flex;align-items:center;gap:4px;background:#0d1a2a;border:1px solid #00d4ff30;border-radius:20px;padding:2px 10px;font-size:11px;color:#00d4ff}

/* 모달 */
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,10,.92);z-index:100;align-items:center;justify-content:center}
#modal.open{display:flex}
.modal-box{background:#10101f;border:1px solid #1c1c35;border-radius:12px;padding:20px;width:560px;max-width:95vw;display:flex;flex-direction:column;gap:14px}
.modal-box h3{color:#00d4ff;font-size:14px;font-weight:700}
.modal-hint{font-size:11px;color:#555;text-align:center}
#region-canvas{display:block;width:100%;height:200px;border-radius:8px;border:1px solid #1c1c35;cursor:crosshair;background:#06060e}
.preset-row{display:flex;gap:6px;flex-wrap:wrap}
.btn-preset{background:#0d1833;color:#00d4ff;border:1px solid #00d4ff25;border-radius:6px;padding:5px 10px;font-size:11px;font-weight:600;cursor:pointer;flex:1;min-width:80px}
.btn-preset:hover{background:#101e40}
.coord-grid{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px}
.coord-field label{display:block;font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.coord-field input{width:100%;background:#090912;border:1px solid #1c1c35;border-radius:6px;padding:6px 8px;color:#dde;font-size:13px;font-weight:600;outline:none;text-align:center}
.coord-field input:focus{border-color:#00d4ff60}
.modal-actions{display:flex;gap:8px;justify-content:flex-end}
.btn-sm{padding:7px 16px;font-size:12px;width:auto}
.btn-cancel{background:#1c1c35;color:#aaa}
.btn-confirm{background:#00d4ff;color:#000;font-weight:700}
.btn-confirm:disabled{background:#1c1c35;color:#444;cursor:default}
</style>
</head>
<body>

<header>
  <h1>⚡ Claude Auto-Approve</h1>
  <span class="pill">v1.4</span>
  <div class="ml-auto">
    <span class="session-badge" id="session-badge">iTerm2 세션 0개</span>
  </div>
</header>

<main>
  <!-- ── 사이드바 ── -->
  <div class="sidebar">

    <!-- 상태 -->
    <div class="panel">
      <h2>상태</h2>
      <div class="stat-row">
        <span class="stat-label"><span class="dot idle" id="dot"></span>모니터</span>
        <span class="stat-val" id="status-text">대기 중</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">권한 승인</span>
        <span class="stat-val" id="approve-count" style="color:#00ff88">0회</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">이어서 진행</span>
        <span class="stat-val" id="cont-count" style="color:#00d4ff">0회</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">30분 재개</span>
        <span class="stat-val" id="stall-count" style="color:#f39c12">0회</span>
      </div>
      <button class="btn btn-start" id="toggle-btn" onclick="toggleMonitor()">▶  모니터 시작</button>
    </div>

    <!-- 영역 -->
    <div class="panel">
      <h2>모니터링 영역</h2>
      <button class="btn btn-region" onclick="openModal()">📐  영역 선택 (드래그)</button>
      <div class="region-info" id="region-info" style="color:#2a2a40">영역 미선택</div>
    </div>

    <!-- 기능 토글 -->
    <div class="panel">
      <h2>기능</h2>
      <div class="toggle-row" onclick="toggleAuto()">
        <div class="sw" id="auto-sw"></div>
        <div class="sw-label">자율 모드<br><span style="color:#555;font-size:10px">토큰 소진→초기화→자동 재개</span></div>
      </div>
      <div class="toggle-row" onclick="toggleCont()">
        <div class="sw green on" id="cont-sw"></div>
        <div class="sw-label">이어서 진행 자동 응답<br><span style="color:#555;font-size:10px">Claude가 물어보면 자동 입력</span></div>
      </div>
      <div class="toggle-row" onclick="toggleStallGit()">
        <div class="sw green on" id="stall-git-sw"></div>
        <div class="sw-label">30분 멈춤 시 Git 자동 커밋<br><span style="color:#555;font-size:10px">재개 명령 전송 + GitHub 푸시</span></div>
      </div>

      <!-- 자율모드 타이머 -->
      <div class="timer-box" id="timer-box">
        <div class="t" id="timer"></div>
        <div class="tl">토큰 초기화까지</div>
      </div>
    </div>

    <!-- 세션 관리 -->
    <div class="panel">
      <h2>세션 관리 <span style="color:#444;font-size:10px;font-weight:400;text-transform:none">승인 / 이어서</span></h2>
      <div id="sess-list"><div style="color:#333;font-size:11px;text-align:center;padding:12px">세션 없음</div></div>
      <button class="btn btn-region" style="margin-top:8px" onclick="refreshSessions()">↻  세션 새로고침</button>
    </div>

    <!-- 설정 -->
    <div class="panel">
      <h2>설정</h2>
      <div class="field">
        <label>응답 지연 (초)</label>
        <input type="number" id="delay" value="1" min="0" max="10" step="0.5" onchange="saveSettings()">
      </div>
      <div class="field">
        <label>이어서 진행 명령어</label>
        <input type="text" id="cont-cmd" value="이어서 진행해줘" onchange="saveSettings()">
      </div>
      <div class="field">
        <label>자율모드 재개 명령어</label>
        <input type="text" id="resume-cmd" value="개발 계속해줘" onchange="saveSettings()">
      </div>
      <div class="field">
        <label>작업 멈춤 감지 시간 (분)</label>
        <input type="number" id="idle-timeout" value="10" min="1" max="60" step="1" onchange="saveSettings()">
        <div style="font-size:10px;color:#444;margin-top:3px">이 시간 동안 변화 없을 때 "이어서" 전송</div>
      </div>
      <button onclick="saveSettings()" style="margin-top:10px;width:100%;padding:8px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer;font-weight:600">💾 설정 저장</button>
      <div id="save-msg" style="font-size:11px;color:#16a34a;text-align:center;margin-top:4px;height:14px"></div>
    </div>

  </div>

  <!-- ── 오른쪽 컬럼 ── -->
  <div class="right-col">
    <div id="preview-wrap">
      <h2>모니터링 미리보기 (2초마다 갱신)</h2>
      <div id="preview-ph">영역 선택 후 미리보기 표시</div>
      <img id="preview" style="display:none" alt="preview">
      <div class="region-info" id="region-info2"></div>
    </div>
    <div id="log-wrap">
      <h2>실시간 로그</h2>
      <div id="log"></div>
    </div>
  </div>
</main>

<!-- 영역 선택 모달 -->
<div id="modal">
  <div class="modal-box">
    <h3>📐 모니터링 영역 선택</h3>

    <!-- 화면 미니맵 (클릭/드래그 가능) -->
    <canvas id="region-canvas"></canvas>
    <div class="modal-hint" id="modal-hint">캔버스에서 드래그하거나 아래 좌표를 입력하세요</div>

    <!-- 프리셋 -->
    <div class="preset-row">
      <button class="btn-preset" onclick="applyPreset('full')">전체화면</button>
      <button class="btn-preset" onclick="applyPreset('left')">왼쪽 절반</button>
      <button class="btn-preset" onclick="applyPreset('right')">오른쪽 절반</button>
      <button class="btn-preset" onclick="applyPreset('top')">상단 절반</button>
      <button class="btn-preset" onclick="applyPreset('bottom')">하단 절반</button>
    </div>

    <!-- 좌표 직접 입력 -->
    <div class="coord-grid">
      <div class="coord-field"><label>X (시작)</label><input id="cx" type="number" value="0" oninput="coordsChanged()"></div>
      <div class="coord-field"><label>Y (시작)</label><input id="cy" type="number" value="0" oninput="coordsChanged()"></div>
      <div class="coord-field"><label>너비 W</label><input id="cw" type="number" value="0" oninput="coordsChanged()"></div>
      <div class="coord-field"><label>높이 H</label><input id="ch" type="number" value="0" oninput="coordsChanged()"></div>
    </div>

    <div class="modal-actions">
      <button class="btn btn-sm btn-cancel" onclick="closeModal()">취소</button>
      <button class="btn btn-sm btn-confirm" id="confirm-btn" onclick="confirmRegion()" disabled>✓ 선택 확인</button>
    </div>
  </div>
</div>

<script>
// ── 상태 ───────────────────────────────────────────────────────────
let autoOn = false, contOn = true, stallGitOn = true;
let selRegion = null;
let origW = 1, origH = 1, dispW = 1, dispH = 1;
let drawing = false, sx = 0, sy = 0, ex = 0, ey = 0;
let canvasImg = null;
let lastLogLen = 0;

// ── 폴링 ───────────────────────────────────────────────────────────
setInterval(pollStatus, 2000);
setInterval(updatePreview, 5000);

async function pollStatus() {
  try {
    const d = await (await fetch('/api/status')).json();
    renderStatus(d);
    renderLogs(d.logs);
    if (!selRegion && d.region) {
      selRegion = {x:d.region[0],y:d.region[1],w:d.region[2],h:d.region[3]};
      showRegionInfo();
    }
    document.getElementById('delay').value = d.delay_sec ?? 1;
    document.getElementById('cont-cmd').value = d.continuation_cmd || '이어서 진행해줘';
    document.getElementById('resume-cmd').value = d.resume_cmd || '개발 계속해줘';
    document.getElementById('idle-timeout').value = Math.round((d.idle_send_timeout ?? 600) / 60);
    autoOn = d.autonomous;
    contOn = d.continuation_mode;
    stallGitOn = d.stall_git ?? true;
    document.getElementById('approve-count').textContent = d.approve_count + '회';
    document.getElementById('cont-count').textContent = d.continuation_count + '회';
    document.getElementById('stall-count').textContent = (d.stall_count || 0) + '회';
    document.getElementById('auto-sw').className = 'sw' + (autoOn ? ' on' : '');
    document.getElementById('cont-sw').className = 'sw green' + (contOn ? ' on' : '');
    document.getElementById('stall-git-sw').className = 'sw green' + (stallGitOn ? ' on' : '');
    document.getElementById('session-badge').textContent = `세션 ${d.active_sessions}개`;
  } catch(e) {}
}

function renderStatus(d) {
  const dot = document.getElementById('dot');
  const txt = document.getElementById('status-text');
  const btn = document.getElementById('toggle-btn');
  const timer = document.getElementById('timer-box');

  if (d.monitoring) {
    btn.className = 'btn btn-stop';
    btn.textContent = '⏹  모니터 중지';
    if (d.last_status === 'detected') {
      dot.className = 'dot warn'; txt.textContent = '⚡ 감지됨!';
    } else if (d.last_status === 'approved') {
      dot.className = 'dot on'; txt.textContent = '✅ 승인 완료';
    } else {
      dot.className = 'dot on'; txt.textContent = '모니터링 중';
    }
  } else {
    btn.className = 'btn btn-start';
    btn.textContent = '▶  모니터 시작';
    dot.className = 'dot idle'; txt.textContent = '대기 중';
  }

  if (d.rate_limit_hit && d.rate_limit_countdown) {
    timer.style.display = 'block';
    document.getElementById('timer').textContent = d.rate_limit_countdown;
  } else {
    timer.style.display = 'none';
  }
}

function renderLogs(logs) {
  if (!logs || logs.length === lastLogLen) return;
  lastLogLen = logs.length;
  const el = document.getElementById('log');
  el.innerHTML = logs.map(l =>
    `<div><span class="l-ts">[${l.ts}]</span> <span class="l-${l.level||'info'}">${esc(l.msg)}</span></div>`
  ).join('');
  el.scrollTop = el.scrollHeight;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function updatePreview() {
  if (!selRegion) return;
  try {
    const d = await (await fetch('/api/preview')).json();
    if (d.image) {
      document.getElementById('preview-ph').style.display = 'none';
      const img = document.getElementById('preview');
      img.style.display = 'block';
      img.src = 'data:image/jpeg;base64,' + d.image;
    }
  } catch(e) {}
}

// ── 제어 ───────────────────────────────────────────────────────────
async function toggleMonitor() {
  const r = await (await fetch('/api/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
  if (r.error) alert(r.error);
}

async function toggleAuto() {
  autoOn = !autoOn;
  await fetch('/api/autonomous',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:autoOn})});
}

async function toggleCont() {
  contOn = !contOn;
  await fetch('/api/continuation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:contOn})});
}

async function toggleStallGit() {
  stallGitOn = !stallGitOn;
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stall_git:stallGitOn})});
}

// ── 세션 관리 ──────────────────────────────────────────────────────
let _sessions = [];
let _pendingSids = new Set(); // POST 중인 세션 — refresh가 덮어쓰지 않도록 보호

async function refreshSessions() {
  try {
    const d = await (await fetch('/api/sessions')).json();
    _mergeSessions(d.sessions || []);
  } catch(e) {}
}

function _mergeSessions(fresh) {
  const oldMap = new Map(_sessions.map(s => [s.sid, s]));
  let changed = _sessions.length !== fresh.length;

  const next = fresh.map(s => {
    const old = oldMap.get(s.sid);
    // POST 전송 중인 세션은 로컬 값 유지
    if (_pendingSids.has(s.sid) && old) {
      return { ...s, approve: old.approve, continuation: old.continuation };
    }
    if (!old || old.approve !== s.approve || old.continuation !== s.continuation || old.title !== s.title) {
      changed = true;
    }
    return s;
  });

  if (!changed) return; // 변화 없으면 리렌더 스킵
  _sessions = next;
  _renderSessions();
}

function _renderSessions() {
  const el = document.getElementById('sess-list');
  if (!_sessions.length) {
    el.innerHTML = '<div style="color:#333;font-size:11px;text-align:center;padding:12px">세션 없음</div>';
    return;
  }
  el.innerHTML = _sessions.map((s, i) => `
    <div class="sess-row">
      <div>
        <div class="sess-title" title="${esc(s.sid)}">${esc(s.title)}</div>
        <div class="sess-type">${s.type}</div>
      </div>
      <div class="sess-toggles">
        <div>
          <div class="mini-sw ${s.approve ? 'on' : ''}" onclick="toggleSessApprove(${i})" title="승인 자동화"></div>
          <div class="sess-label">승인</div>
        </div>
        <div>
          <div class="mini-sw green ${s.continuation ? 'on' : ''}" onclick="toggleSessCont(${i})" title="이어서 진행"></div>
          <div class="sess-label">이어서</div>
        </div>
      </div>
    </div>`).join('');
}

// 버튼용 — 강제 전체 재렌더
function renderSessions() { _renderSessions(); }

async function toggleSessApprove(i) {
  const s = _sessions[i];
  s.approve = !s.approve;
  _pendingSids.add(s.sid);
  _renderSessions();
  try {
    await fetch('/api/session-config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({sid: s.sid, approve: s.approve})});
  } finally {
    _pendingSids.delete(s.sid);
  }
}

async function toggleSessCont(i) {
  const s = _sessions[i];
  s.continuation = !s.continuation;
  _pendingSids.add(s.sid);
  _renderSessions();
  try {
    await fetch('/api/session-config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({sid: s.sid, continuation: s.continuation})});
  } finally {
    _pendingSids.delete(s.sid);
  }
}

// 최초 로드 + 2초마다 자동 갱신 (status poll과 동일 주기)
refreshSessions();
setInterval(refreshSessions, 2000);

async function saveSettings() {
  const body = {
    delay_sec: parseFloat(document.getElementById('delay').value)||1,
    continuation_cmd: document.getElementById('cont-cmd').value,
    resume_cmd: document.getElementById('resume-cmd').value,
    idle_send_timeout: (parseFloat(document.getElementById('idle-timeout').value)||10) * 60,
  };
  const res = await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const msg = document.getElementById('save-msg');
  if (msg) {
    msg.textContent = res.ok ? '✓ 저장됨' : '저장 실패';
    setTimeout(() => { msg.textContent = ''; }, 2000);
  }
}

// ── 영역 선택 모달 ──────────────────────────────────────────────────
let screenW = 1440, screenH = 900;
let rDragging = false, rSx = 0, rSy = 0, rEx = 0, rEy = 0;

async function openModal() {
  document.getElementById('modal').classList.add('open');
  document.getElementById('modal-hint').textContent = '화면 로딩 중...';
  let d = {};
  try { d = await (await fetch('/api/fullscreen')).json(); } catch(e) {}
  if (d.screen_w) { screenW = d.screen_w; screenH = d.screen_h; }

  if (d.image) {
    // ── 스크린샷 있으면: 실제 화면 위에서 드래그 ──
    origW = d.orig_w; origH = d.orig_h; dispW = d.disp_w; dispH = d.disp_h;
    const cvs = document.getElementById('region-canvas');
    const ctx  = cvs.getContext('2d');
    const mw = Math.min(window.innerWidth * 0.88, 1300);
    const ratio = Math.min(mw / d.disp_w, 520 / d.disp_h);
    cvs.width  = Math.round(d.disp_w * ratio);
    cvs.height = Math.round(d.disp_h * ratio);
    const img = new Image();
    img.onload = () => {
      ctx.drawImage(img, 0, 0, cvs.width, cvs.height);
      canvasImg = ctx.getImageData(0, 0, cvs.width, cvs.height);
      document.getElementById('modal-hint').textContent =
        `화면에서 드래그하여 영역 선택 (${d.orig_w}×${d.orig_h})`;
      bindScreenshotCanvas(cvs, ctx);
    };
    img.src = 'data:image/jpeg;base64,' + d.image;
  } else {
    // ── 스크린샷 없으면: 미니맵 + 좌표 입력 ──
    initRegionCanvas();
    if (selRegion) {
      document.getElementById('cx').value = selRegion.x;
      document.getElementById('cy').value = selRegion.y;
      document.getElementById('cw').value = selRegion.w;
      document.getElementById('ch').value = selRegion.h;
      document.getElementById('confirm-btn').disabled = false;
      drawRegionCanvas();
    } else {
      applyPreset('full');
    }
    document.getElementById('modal-hint').textContent =
      '⚠ 화면 기록 권한 없음 — 좌표 직접 입력 또는 프리셋 사용';
  }
}

// 실제 스크린샷 위에서 드래그
function bindScreenshotCanvas(cvs, ctx) {
  let ssx = 0, ssy = 0, sex = 0, sey = 0, drawing = false;
  const scX = origW / cvs.width, scY = origH / cvs.height;
  cvs.onmousedown = e => {
    drawing = true;
    const rc = cvs.getBoundingClientRect();
    ssx = e.clientX - rc.left; ssy = e.clientY - rc.top;
    sex = ssx; sey = ssy;
  };
  cvs.onmousemove = e => {
    if (!drawing) return;
    const rc = cvs.getBoundingClientRect();
    sex = e.clientX - rc.left; sey = e.clientY - rc.top;
    if (canvasImg) ctx.putImageData(canvasImg, 0, 0);
    const rx = Math.min(ssx,sex), ry = Math.min(ssy,sey);
    const rw = Math.abs(sex-ssx), rh = Math.abs(sey-ssy);
    ctx.fillStyle = 'rgba(0,212,255,0.08)'; ctx.fillRect(rx,ry,rw,rh);
    ctx.strokeStyle = '#00d4ff'; ctx.lineWidth = 2; ctx.strokeRect(rx,ry,rw,rh);
    const ox = Math.round(Math.min(ssx,sex)*scX), oy = Math.round(Math.min(ssy,sey)*scY);
    const ow2 = Math.round(Math.abs(sex-ssx)*scX), oh2 = Math.round(Math.abs(sey-ssy)*scY);
    document.getElementById('cx').value = ox;
    document.getElementById('cy').value = oy;
    document.getElementById('cw').value = ow2;
    document.getElementById('ch').value = oh2;
  };
  cvs.onmouseup = e => {
    drawing = false;
    const w = parseInt(document.getElementById('cw').value)||0;
    const h = parseInt(document.getElementById('ch').value)||0;
    if (w > 10 && h > 10) {
      document.getElementById('confirm-btn').disabled = false;
      document.getElementById('modal-hint').textContent = `선택: ${w}×${h} px — 확인 버튼 클릭`;
    }
  };
}

function initRegionCanvas() {
  const cvs = document.getElementById('region-canvas');
  const rect = cvs.getBoundingClientRect();
  cvs.width  = rect.width  || 520;
  cvs.height = rect.height || 200;
  drawRegionCanvas();
  cvs.onmousedown = e => {
    rDragging = true;
    const rc = cvs.getBoundingClientRect();
    rSx = e.clientX - rc.left; rSy = e.clientY - rc.top;
    rEx = rSx; rEy = rSy;
  };
  cvs.onmousemove = e => {
    if (!rDragging) return;
    const rc = cvs.getBoundingClientRect();
    rEx = e.clientX - rc.left; rEy = e.clientY - rc.top;
    // 캔버스 좌표 → 화면 좌표
    const scX = screenW / cvs.width, scY = screenH / cvs.height;
    const x = Math.round(Math.min(rSx,rEx)*scX);
    const y = Math.round(Math.min(rSy,rEy)*scY);
    const w = Math.round(Math.abs(rEx-rSx)*scX);
    const h = Math.round(Math.abs(rEy-rSy)*scY);
    document.getElementById('cx').value = x;
    document.getElementById('cy').value = y;
    document.getElementById('cw').value = w;
    document.getElementById('ch').value = h;
    drawRegionCanvas();
  };
  cvs.onmouseup = e => {
    rDragging = false;
    const w = parseInt(document.getElementById('cw').value)||0;
    const h = parseInt(document.getElementById('ch').value)||0;
    if (w > 10 && h > 10) {
      document.getElementById('confirm-btn').disabled = false;
      document.getElementById('modal-hint').textContent =
        `선택: ${w} × ${h} px (화면 ${screenW}×${screenH} 기준)`;
    }
  };
}

function drawRegionCanvas() {
  const cvs = document.getElementById('region-canvas');
  const ctx = cvs.getContext('2d');
  const cw = cvs.width, ch = cvs.height;
  // 배경 (화면 표현)
  ctx.fillStyle = '#06060e';
  ctx.fillRect(0, 0, cw, ch);
  // 그리드 선
  ctx.strokeStyle = '#1a1a30';
  ctx.lineWidth = 0.5;
  for (let x = 0; x < cw; x += cw/8) { ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,ch); ctx.stroke(); }
  for (let y = 0; y < ch; y += ch/5) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(cw,y); ctx.stroke(); }
  // 화면 테두리
  ctx.strokeStyle = '#2a2a50'; ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, cw-1, ch-1);
  // 화면 크기 표시
  ctx.fillStyle = '#2a2a50'; ctx.font = '10px monospace'; ctx.textAlign = 'right';
  ctx.fillText(`${screenW}×${screenH}`, cw-6, ch-6);
  // 선택 영역 그리기
  const x = parseInt(document.getElementById('cx').value)||0;
  const y = parseInt(document.getElementById('cy').value)||0;
  const w = parseInt(document.getElementById('cw').value)||0;
  const h = parseInt(document.getElementById('ch').value)||0;
  if (w > 0 && h > 0) {
    const scX = cw / screenW, scY = ch / screenH;
    const rx = x*scX, ry = y*scY, rw = w*scX, rh = h*scY;
    ctx.fillStyle = 'rgba(0,212,255,0.12)';
    ctx.fillRect(rx, ry, rw, rh);
    ctx.strokeStyle = '#00d4ff'; ctx.lineWidth = 1.5;
    ctx.strokeRect(rx, ry, rw, rh);
    // 선택 크기 텍스트
    ctx.fillStyle = '#00d4ff'; ctx.font = 'bold 11px monospace'; ctx.textAlign = 'center';
    ctx.fillText(`${w}×${h}`, rx + rw/2, ry + rh/2 + 4);
  }
}

function coordsChanged() {
  drawRegionCanvas();
  const w = parseInt(document.getElementById('cw').value)||0;
  const h = parseInt(document.getElementById('ch').value)||0;
  document.getElementById('confirm-btn').disabled = !(w > 0 && h > 0);
  if (w > 0 && h > 0) {
    const x = parseInt(document.getElementById('cx').value)||0;
    const y = parseInt(document.getElementById('cy').value)||0;
    document.getElementById('modal-hint').textContent = `선택: (${x}, ${y}) ${w}×${h} px`;
  }
}

function applyPreset(type) {
  const sw = screenW, sh = screenH;
  const presets = {
    full:   [0,   0,   sw,    sh],
    left:   [0,   0,   sw/2,  sh],
    right:  [sw/2,0,   sw/2,  sh],
    top:    [0,   0,   sw,    sh/2],
    bottom: [0,   sh/2,sw,    sh/2],
  };
  const [x,y,w,h] = presets[type].map(Math.round);
  document.getElementById('cx').value = x;
  document.getElementById('cy').value = y;
  document.getElementById('cw').value = w;
  document.getElementById('ch').value = h;
  coordsChanged();
  document.getElementById('confirm-btn').disabled = false;
}

async function confirmRegion() {
  const x = parseInt(document.getElementById('cx').value)||0;
  const y = parseInt(document.getElementById('cy').value)||0;
  const w = parseInt(document.getElementById('cw').value)||0;
  const h = parseInt(document.getElementById('ch').value)||0;
  if (w <= 0 || h <= 0) return;
  selRegion = {x,y,w,h};
  await fetch('/api/region',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({region:[x,y,w,h]})});
  showRegionInfo();
  closeModal();
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
  document.getElementById('confirm-btn').disabled = true;
}

function showRegionInfo() {
  if (!selRegion) return;
  const t = `(${selRegion.x}, ${selRegion.y})  ${selRegion.w} × ${selRegion.h} px`;
  document.getElementById('region-info').textContent = t;
  document.getElementById('region-info').style.color = '#00d4ff';
  document.getElementById('region-info2').textContent = t;
}

pollStatus();
</script>
</body>
</html>"""


# ─── 모바일 UI ─────────────────────────────────────────────────────────
MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Claude Auto-Approve</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:#090912;color:#dde;font-family:-apple-system,sans-serif;padding:16px;max-width:480px;margin:0 auto}
h1{font-size:18px;font-weight:700;color:#00d4ff;margin-bottom:4px}
.sub{font-size:12px;color:#444;margin-bottom:20px}
.card{background:#10101f;border:1px solid #1c1c35;border-radius:12px;padding:16px;margin-bottom:12px}
.card h2{font-size:11px;font-weight:600;color:#00d4ff;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #1c1c35}
.row:last-child{border-bottom:none}
.lbl{font-size:12px;color:#888}
.val{font-size:14px;font-weight:700}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#333;margin-right:6px}
.dot.on{background:#00ff88;box-shadow:0 0 6px #00ff8877}
.dot.warn{background:#f39c12;animation:blink .7s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.btn{display:block;width:100%;padding:14px;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;margin-bottom:10px;letter-spacing:.3px}
.btn-green{background:#0d3322;color:#00ff88;border:1px solid #00ff8840}
.btn-red{background:#33100d;color:#ff7070;border:1px solid #ff707040}
.btn-blue{background:#0d1833;color:#00d4ff;border:1px solid #00d4ff40}
.btn-gray{background:#1a1a2e;color:#aaa;border:1px solid #2a2a4a}
.quick-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.quick-btn{padding:12px 8px;border:none;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer;background:#1a1a2e;color:#dde;border:1px solid #2a2a4a}
.quick-btn:active{opacity:.7}
.inp{width:100%;background:#090912;border:1px solid #1c1c35;border-radius:8px;padding:12px;color:#dde;font-size:14px;outline:none;margin-bottom:8px}
.inp:focus{border-color:#00d4ff60}
#log{background:#05050d;border-radius:8px;padding:10px;height:200px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.6}
.l-ok{color:#00e87a}.l-warn{color:#d4930a}.l-error{color:#c0392b}.l-info{color:#3a7}.l-ts{color:#2a2a40}
.timer{text-align:center;padding:10px;background:#1a0f00;border-radius:8px;margin-bottom:10px;display:none}
.timer .t{font-size:28px;font-weight:700;color:#f39c12;letter-spacing:3px;font-variant-numeric:tabular-nums}
.timer .tl{font-size:11px;color:#6a4a00;margin-top:2px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#00d4ff;color:#000;padding:10px 20px;border-radius:20px;font-weight:700;font-size:13px;opacity:0;transition:.3s;pointer-events:none}
.toast.show{opacity:1}
</style>
</head>
<body>
<h1>⚡ Claude Auto-Approve</h1>
<div class="sub" id="sub">모바일 제어 패널</div>

<!-- 상태 -->
<div class="card">
  <h2>상태</h2>
  <div class="row"><span class="lbl"><span class="dot" id="dot"></span>모니터</span><span class="val" id="status">-</span></div>
  <div class="row"><span class="lbl">권한 승인</span><span class="val" style="color:#00ff88" id="approve">-</span></div>
  <div class="row"><span class="lbl">이어서 진행</span><span class="val" style="color:#00d4ff" id="cont">-</span></div>
  <div class="row"><span class="lbl">30분 재개</span><span class="val" style="color:#f39c12" id="stall">-</span></div>
</div>

<!-- 타이머 -->
<div class="timer" id="timer-box">
  <div class="t" id="timer">--:--:--</div>
  <div class="tl">토큰 초기화까지</div>
</div>

<!-- 모니터 제어 -->
<div class="card">
  <h2>제어</h2>
  <button class="btn btn-green" id="toggle-btn" onclick="toggleMonitor()">▶  모니터 시작</button>
</div>

<!-- 빠른 명령 -->
<div class="card">
  <h2>빠른 명령</h2>
  <div class="quick-grid">
    <button class="quick-btn" onclick="sendCmd('개발 계속해줘')">🔄 계속해줘</button>
    <button class="quick-btn" onclick="sendCmd('이어서 진행해줘')">▶️ 이어서</button>
    <button class="quick-btn" onclick="sendCmd('현재 상태 알려줘')">📊 상태확인</button>
    <button class="quick-btn" onclick="sendCmd('잠깐 멈춰줘')">⏸ 일시정지</button>
  </div>
  <input class="inp" id="custom-cmd" placeholder="커스텀 명령 입력..." type="text">
  <button class="btn btn-blue" onclick="sendCustom()">📨 전송</button>
</div>

<!-- 로그 -->
<div class="card">
  <h2>실시간 로그</h2>
  <div id="log"></div>
</div>

<div class="toast" id="toast"></div>

<script>
let lastLogLen = 0;

async function poll() {
  try {
    const d = await (await fetch('/api/status')).json();
    const dot = document.getElementById('dot');
    const status = document.getElementById('status');
    const btn = document.getElementById('toggle-btn');
    if (d.monitoring) {
      dot.className = 'dot on'; status.textContent = '모니터링 중';
      btn.className = 'btn btn-red'; btn.textContent = '⏹  모니터 중지';
    } else {
      dot.className = 'dot'; status.textContent = '대기 중';
      btn.className = 'btn btn-green'; btn.textContent = '▶  모니터 시작';
    }
    if (d.rate_limit_hit && d.rate_limit_countdown) {
      document.getElementById('timer-box').style.display = 'block';
      document.getElementById('timer').textContent = d.rate_limit_countdown;
      dot.className = 'dot warn'; status.textContent = '토큰 한도';
    } else {
      document.getElementById('timer-box').style.display = 'none';
    }
    document.getElementById('approve').textContent = d.approve_count + '회';
    document.getElementById('cont').textContent = d.continuation_count + '회';
    document.getElementById('stall').textContent = (d.stall_count||0) + '회';
    if (d.logs && d.logs.length !== lastLogLen) {
      lastLogLen = d.logs.length;
      const el = document.getElementById('log');
      el.innerHTML = d.logs.slice(-40).map(l =>
        `<div><span class="l-ts">[${l.ts}]</span> <span class="l-${l.level||'info'}">${esc(l.msg)}</span></div>`
      ).join('');
      el.scrollTop = el.scrollHeight;
    }
  } catch(e) {}
}

async function toggleMonitor() {
  const r = await (await fetch('/api/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
  if (r.error) toast(r.error, true);
}

async function sendCmd(cmd) {
  const r = await (await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd})})).json();
  toast(r.ok ? `✅ "${cmd}" 전송 완료` : '전송 실패');
}

async function sendCustom() {
  const cmd = document.getElementById('custom-cmd').value.trim();
  if (!cmd) return;
  await sendCmd(cmd);
  document.getElementById('custom-cmd').value = '';
}

document.getElementById('custom-cmd').addEventListener('keydown', e => {
  if (e.key === 'Enter') sendCustom();
});

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function toast(msg, err=false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = err ? '#e74c3c' : '#00d4ff';
  el.style.color = err ? '#fff' : '#000';
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

poll();
setInterval(poll, 3000);
</script>
</body>
</html>"""


# ─── 메인 ──────────────────────────────────────────────────────────────
def main():
    cmux_status = f"cmux 내부 ✓ (surface={os.environ.get('CMUX_SURFACE_ID','?')[:8]})" if _cmux_inside else "cmux 외부 ⚠"
    ip = _local_ip()
    slack_status = '✓ 설정됨' if SLACK_WEBHOOK_URL else '⚠ 미설정 (SLACK_WEBHOOK_URL 환경변수 필요)'

    if not _cmux_inside and os.path.exists(CMUX_SOCK):
        print("⚠️  cmux 외부에서 실행 중 — cmux 탭에서 실행하면 세션 모니터링 가능")

    _, ok = osascript('tell application "System Events" to get name of first process whose frontmost is true')
    if not ok:
        print("⚠️  접근성 권한 필요: 시스템 설정 → 개인정보 보호 → 손쉬운 사용 → 터미널 허용")

    print(f"""
╔══════════════════════════════════════════════════╗
║       Claude Auto-Approve  v1.4                  ║
╠══════════════════════════════════════════════════╣
║  PC      : http://localhost:{PORT}               ║
║  모바일  : http://{ip}:{PORT}/m                 ║
║  (같은 WiFi에서 접속)                            ║
╠══════════════════════════════════════════════════╣
║  Slack: {slack_status}
║  cmux : {cmux_status}
╚══════════════════════════════════════════════════╝
""")

    slack_notify(
        f'🟢 *Claude Auto-Approve 시작*\n모바일 제어: http://{ip}:{PORT}/m',
        '🟢'
    )

    server = HTTPServer((HOST, PORT), Handler)
    threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(f'http://localhost:{PORT}')), daemon=True).start()

    # 저장된 설정에서 자동 재개
    if _saved.get('autostart') and state.get('region'):
        def _autostart():
            time.sleep(2)
            with state_lock:
                if not state['monitoring']:
                    state['monitoring'] = True
                    state['last_status'] = 'monitoring'
                    if _saved.get('autonomous_mode'):
                        state['autonomous_mode'] = True
            log('설정 복원: 모니터링 자동 재개', 'ok')
            threading.Thread(target=monitor_loop, daemon=True).start()
        threading.Thread(target=_autostart, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        slack_notify('🔴 *Claude Auto-Approve 종료*', '🔴')
        print('\n종료됨')


if __name__ == '__main__':
    main()
