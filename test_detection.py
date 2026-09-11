#!/usr/bin/env python3
"""app.py 감지 로직 회귀 테스트.

이 데몬은 화면 문자열을 읽어 판단하기 때문에, Claude Code가 UI를 바꾸면
에러 하나 없이 조용히 죽는다. 실제로 2026-08에 스피너가 ✳ → ◐ 로 바뀌면서
한 달 넘게 세션을 하나도 못 보는 상태로 돌고 있었다.

그래서 픽스처는 전부 **실제 터미널에서 뜬 화면**을 붙여넣은 것이다.
UI가 또 바뀌어 이 테스트가 깨지면, 그게 곧 알림이다.

  python3 test_detection.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_app():
    spec = importlib.util.spec_from_file_location('aa', os.path.join(HERE, 'app.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


app = load_app()

# ─── 실제 화면 픽스처 ────────────────────────────────────────────────────
# 2026-09-11 cmux surface.read_text 로 그대로 떠온 것

BUSY = """\
✽ Unravelling… (59s · ↓ 2.8k tokens)
  ⎿  Tip: Use /btw to ask a quick side question without interrupting
     Claude's current work
                              ✔ Update installed · Restart to update
──────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────

  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""

IDLE = """\
  ⎿  Wrote 1 lines to run_all.sh
                              ✔ Update installed · Restart to update
──────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────

  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""

IDLE_TYPING = """\
                              ✔ Update installed · Restart to update
──────────────────────────────────────────────────────────────────────
❯ 치트시트 만들어줘
──────────────────────────────────────────────────────────────────────

  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""

DIALOG = """\
╭──────────────────────────────────────────────────────────────────╮
│ Bash command                                                     │
│                                                                  │
│   rm -rf /tmp/scratch                                            │
│   Remove the scratch directory                                   │
│                                                                  │
│ Do you want to proceed?                                          │
│ ❯ 1. Yes                                                         │
│   2. Yes, and don't ask again for rm commands in this project    │
│   3. No, and tell Claude what to do differently (esc)            │
╰──────────────────────────────────────────────────────────────────╯
"""

SURVEY = """\
╭──────────────────────────────────────────────────────────────────╮
│ How is Claude doing this session?                                │
│   1  Bad    2  Poor    3  OK    4  Good    5  Great              │
│   0  Dismiss                                                     │
╰──────────────────────────────────────────────────────────────────╯
"""

CONTINUATION = """\
  Phase 2 결제 연동까지 마쳤습니다. 테스트 28개 전부 통과했습니다.

  다음은 Phase 3 리텐션 작업입니다. 이어서 진행할까요?
──────────────────────────────────────────────────────────────────────
❯
"""

RATE_LIMIT_CLAUDE = """\
  You've hit your usage limit. Your limit will reset at 7pm.

  · resets 7pm (Asia/Seoul)
"""

RATE_LIMIT_GEMINI = """\
⚠ Individual quota reached. Please upgrade your subscription.
  Resets in 52m50s
  Error ID: 8f2a10c4
"""

APPROACHING = """\
  Approaching usage limit · resets 11pm
──────────────────────────────────────────────────────────────────────
❯
"""

SHELL = """\
beakjong-woo@Mac ~ % ls
Desktop  Documents  Downloads
beakjong-woo@Mac ~ %
"""

# 자기 자신(데몬)의 로그 화면 — 절대 Claude 세션으로 오인하면 안 된다
DAEMON_LOG = """\
[17:30:30] 모니터링 루프 시작
[17:30:32] ✅ 승인 → cmux (총 12회)
[17:30:34] 📨 이어서 진행 → 세션 cmux:sur... "이어서 진행해줘" (총 3회)
"""


# ─── 테스트 러너 ─────────────────────────────────────────────────────────
_results = []


def check(name, got, want):
    ok = got == want
    _results.append((ok, name, got, want))
    print(f"{'✅' if ok else '❌'} {name}"
          + ('' if ok else f"   기대={want} 결과={got}"))
    return ok


def section(t):
    print(f"\n── {t} " + "─" * max(0, 56 - len(t)))


# ─── 1. 세션 제목 판별 ───────────────────────────────────────────────────
section("제목으로 Claude 세션 판별")
check("◐ 원형 스피너 (2026-09 현행)", app._is_claude_title('◐ Claude 공유 기능 확인'), True)
check("◑ 원형 스피너 반대 위상", app._is_claude_title('◑ 작업 중'), True)
check("✳ 구버전 스피너", app._is_claude_title('✳ Claude Code'), True)
check("⠙ 점자 스피너", app._is_claude_title('⠙ 빌드 중'), True)
check("✽ sparkle 스피너", app._is_claude_title('✽ Unravelling'), True)
check("셸 탭 ~", app._is_claude_title('~'), False)
check("셸 탭 ~/ai-cases", app._is_claude_title('~/ai-cases'), False)
check("사람이 붙인 이름", app._is_claude_title('클로드 오토어프로브'), False)
check("빈 제목", app._is_claude_title(''), False)

# ─── 2. 내용으로 Claude 세션 판별 (제목이 또 바뀔 때의 안전망) ──────────
section("내용으로 Claude 세션 판별 (폴백)")
check("작업 중 화면", app._looks_like_claude_content(BUSY), True)
check("대기 중 화면", app._looks_like_claude_content(IDLE), True)
check("권한 다이얼로그", app._looks_like_claude_content(DIALOG), True)
check("순수 셸", app._looks_like_claude_content(SHELL), False)
check("데몬 자기 로그", app._looks_like_claude_content(DAEMON_LOG), False)

# ─── 3. 대기/작업 상태 ───────────────────────────────────────────────────
section("대기(idle) 판별")
check("입력창 비어 있고 작업 안 함 → 대기", app.is_prompt_idle(IDLE), True)
check("작업 중(스피너+경과시간) → 대기 아님", app.is_prompt_idle(BUSY), False)
check("사용자가 타이핑 중 → 대기 아님", app.is_prompt_idle(IDLE_TYPING), False)
check("다이얼로그 떠 있음 → 대기 아님", app.is_prompt_idle(DIALOG), False)

# ─── 4. 다이얼로그 ───────────────────────────────────────────────────────
section("권한 다이얼로그 감지")
check("실제 Bash 승인 다이얼로그", app.is_dialog(DIALOG), True)
check("대기 화면은 다이얼로그 아님", app.is_dialog(IDLE), False)
check("작업 중 화면은 다이얼로그 아님", app.is_dialog(BUSY), False)
check("피드백 설문 감지", app.is_feedback_survey(SURVEY), True)
check("설문 아닌 화면", app.is_feedback_survey(DIALOG), False)

# ─── 5. 이어서 진행 질문 ─────────────────────────────────────────────────
section("이어서 진행 질문 감지")
check("한국어 '이어서 진행할까요'", app.is_continuation(CONTINUATION), True)
check("영어 'shall i continue'", app.is_continuation('Done.\n\nShall I continue?\n❯'), True)
check("일반 대기 화면", app.is_continuation(IDLE), False)

# ─── 6. 사용량 한도 ──────────────────────────────────────────────────────
section("사용량 한도 감지 · 리셋 시각 파싱")
check("Claude 한도", app.is_rate_limit(RATE_LIMIT_CLAUDE), True)
check("Gemini 한도", app.is_rate_limit(RATE_LIMIT_GEMINI), True)
check("'approaching' 경고는 한도 아님", app.is_rate_limit(APPROACHING), False)
check("정상 화면", app.is_rate_limit(IDLE), False)
check("Claude 로 분류", app._session_kind(RATE_LIMIT_CLAUDE), 'claude')
check("Gemini 로 분류", app._session_kind(RATE_LIMIT_GEMINI), 'gemini')

_rel = app.parse_reset_epoch('Resets in 52m50s')
check("상대시각 52m50s → 3170초 근사", 3160 <= (_rel - time.time()) <= 3180, True)
_abs = app.parse_reset_epoch('resets 7pm (Asia/Seoul)')
check("절대시각 7pm 파싱됨(미래)", _abs > time.time(), True)
check("파싱 불가 시 0", app.parse_reset_epoch('no reset info here'), 0.0)


# ─── 7. 자동 git 커밋 (임시 저장소에서 실제 실행) ────────────────────────
section("자동 git 커밋 · 실제 동작")


def run_git_test():
    with tempfile.TemporaryDirectory() as d:
        def g(*a):
            return subprocess.run(['git', '-C', d, *a], capture_output=True, text=True)

        g('init', '-q')
        g('config', 'user.email', 'test@local')
        g('config', 'user.name', 'test')
        g('config', 'commit.gpgsign', 'false')
        open(os.path.join(d, 'a.txt'), 'w').write('hello\n')
        g('add', '-A')
        g('commit', '-qm', 'init')
        base = g('rev-list', '--count', 'HEAD').stdout.strip()

        # 변경 없음 → 커밋하지 않아야 한다
        app.auto_git_commit_push(d)
        n1 = g('rev-list', '--count', 'HEAD').stdout.strip()
        check("변경 없으면 커밋 안 함", n1, base)

        # 변경 있음 → 커밋해야 한다 (push 는 remote 가 없어 실패하지만 커밋은 남는다)
        open(os.path.join(d, 'b.txt'), 'w').write('world\n')
        app.auto_git_commit_push(d)
        n2 = int(g('rev-list', '--count', 'HEAD').stdout.strip())
        check("변경 있으면 커밋함", n2, int(base) + 1)

        msg = g('log', '-1', '--pretty=%s').stdout.strip()
        check("커밋 메시지에 [Auto] 접두사", msg.startswith('[Auto]'), True)
        check("워킹 트리 깨끗해짐", g('status', '--porcelain').stdout.strip(), '')


run_git_test()

# ─── 결과 ────────────────────────────────────────────────────────────────
passed = sum(1 for ok, *_ in _results if ok)
failed = len(_results) - passed
print("\n" + "═" * 62)
print(f"  통과 {passed} / 실패 {failed}   (전체 {len(_results)})")
print("═" * 62)
if failed:
    print("\n실패 목록:")
    for ok, name, got, want in _results:
        if not ok:
            print(f"  · {name}: 기대={want} 결과={got}")
sys.exit(1 if failed else 0)
