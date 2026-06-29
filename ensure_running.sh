#!/bin/bash
# 이미 실행 중이면 종료
if lsof -ti :17654 &>/dev/null; then
    exit 0
fi

# cmux가 실행 중인지 확인
if ! pgrep -x cmux &>/dev/null; then
    exit 0
fi

# cmux 새 탭에서 앱 시작
SURF=$(cmux new-surface 2>/dev/null | grep -oE 'surface:[0-9]+')
if [ -n "$SURF" ]; then
    cmux rename-tab --surface "$SURF" "클로드 오토어프로브" 2>/dev/null
    cmux send --surface "$SURF" "cd ~/claude-auto-approve && python3 app.py"
    cmux send-key --surface "$SURF" enter
fi
