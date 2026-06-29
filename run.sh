#!/bin/bash
cd "$(dirname "$0")"

# 이미 실행 중이면 종료
if lsof -ti :17654 &>/dev/null; then
    echo "이미 실행 중 (port 17654)"
    exit 0
fi

# cmux 내부에서 실행 중 → 바로 시작
if [ -n "$CMUX_SURFACE_ID" ]; then
    exec python3 app.py
fi

# cmux가 실행 중이면 새 탭에서 시작 (cmux 세션 감지를 위해 내부 실행 필요)
if command -v cmux &>/dev/null && pgrep -x cmux &>/dev/null; then
    SURF=$(cmux new-surface 2>/dev/null | grep -oE 'surface:[0-9]+')
    if [ -n "$SURF" ]; then
        cmux rename-tab --surface "$SURF" "클로드 오토어프로브" 2>/dev/null
        cmux send --surface "$SURF" "cd ~/claude-auto-approve && python3 app.py"
        cmux send-key --surface "$SURF" enter
        echo "✅ cmux 탭에서 시작됨 (surface: $SURF)"
        exit 0
    fi
fi

# 폴백: 직접 실행 (cmux 세션 감지 불가)
echo "⚠️  cmux 외부에서 실행 중 — cmux 세션 감지 불가"
exec python3 app.py
