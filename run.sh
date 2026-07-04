#!/bin/bash
cd "$(dirname "$0")"

# 이미 실행 중이면 종료 (LISTEN 소켓만 검사)
if lsof -nP -iTCP:17654 -sTCP:LISTEN -t &>/dev/null; then
    echo "이미 실행 중 (port 17654)"
    exit 0
fi

# cmux 내부에서 실행 중 → 바로 시작
if [ -n "$CMUX_SURFACE_ID" ]; then
    exec python3 app.py
fi

# cmux CLI 경로: PATH 우선, 없으면 번들 경로
CMUX_BIN="/Applications/cmux.app/Contents/Resources/bin/cmux"
command -v cmux &>/dev/null && CMUX_BIN="$(command -v cmux)"

# cmux 새 탭 생성을 직접 시도 (pgrep은 샌드박스/launchd 환경에서 신뢰 불가)
if [ -x "$CMUX_BIN" ]; then
    SURF=$("$CMUX_BIN" new-surface 2>/dev/null | grep -oE 'surface:[0-9]+')
    if [ -n "$SURF" ]; then
        "$CMUX_BIN" rename-tab --surface "$SURF" "클로드 오토어프로브" 2>/dev/null
        "$CMUX_BIN" send --surface "$SURF" "cd ~/claude-auto-approve && exec python3 app.py"
        "$CMUX_BIN" send-key --surface "$SURF" enter
        echo "✅ cmux 탭에서 시작됨 (surface: $SURF)"
        exit 0
    fi
fi

# 폴백: 직접 실행 (cmux 세션 감지 불가)
echo "⚠️  cmux 외부에서 실행 중 — cmux 세션 감지 불가"
exec python3 app.py
