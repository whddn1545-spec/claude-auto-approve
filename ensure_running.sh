#!/bin/bash
# 이미 실행 중이면 종료 (LISTEN 소켓만 검사 — 클라이언트 연결(브라우저 등)은 제외)
if lsof -nP -iTCP:17654 -sTCP:LISTEN -t &>/dev/null; then
    exit 0
fi

# cmux CLI 경로: PATH 우선, 없으면 번들 경로 (launchd 컨텍스트엔 PATH에 cmux 없음)
CMUX_BIN="/Applications/cmux.app/Contents/Resources/bin/cmux"
command -v cmux &>/dev/null && CMUX_BIN="$(command -v cmux)"

start_fallback() {
    cd "$(dirname "$0")"
    nohup python3 app.py >> /tmp/claude-auto-approve-fallback.log 2>&1 &
    disown
}

# cmux 새 탭 생성을 직접 시도 — 성공 여부 자체가 "cmux 접근 가능" 판별
# (pgrep은 샌드박스/launchd 환경에서 신뢰 불가. cmux 세션 감지는 라이브 cmux 탭
#  내부 프로세스만 가능하므로, 탭 생성이 되면 반드시 탭 안에서 실행해야 한다)
if [ -x "$CMUX_BIN" ]; then
    SURF=$("$CMUX_BIN" new-surface 2>/dev/null | grep -oE 'surface:[0-9]+')
    if [ -n "$SURF" ]; then
        "$CMUX_BIN" rename-tab --surface "$SURF" "클로드 오토어프로브" 2>/dev/null
        "$CMUX_BIN" send --surface "$SURF" "cd ~/claude-auto-approve && exec python3 app.py"
        "$CMUX_BIN" send-key --surface "$SURF" enter
        exit 0
    fi
fi

# cmux 탭 생성 실패 (cmux 미실행이거나 cmux 트리 밖에서 호출됨) → 폴백 실행
# 이 모드에선 cmux 세션 감지는 안 되지만 UI/iTerm2 감지는 동작
start_fallback
