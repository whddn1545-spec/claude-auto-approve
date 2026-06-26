#!/bin/bash
# claude-auto-approve를 cmux 내에서 실행되도록 보장하는 스크립트
# launchd가 5분마다 호출 → 앱이 죽어있으면 cmux 새 탭에서 재시작

# 이미 실행 중이면 종료
if lsof -ti :17654 &>/dev/null; then
    exit 0
fi

# cmux가 실행 중인지 확인
if ! pgrep -x cmux &>/dev/null; then
    exit 0  # cmux 미실행 시 건너뜀
fi

# cmux 새 탭에서 앱 시작
osascript << 'APPLESCRIPT'
tell application "cmux" to activate
delay 1

tell application "System Events"
    tell process "cmux"
        -- Cmd+T: 새 탭 (shell 상태인 탭 열림)
        keystroke "t" using {command down}
        delay 2
        -- 현재 포커스가 입력창인지 확인 후 실행
        keystroke "cd ~/claude-auto-approve && python3 app.py"
        key code 36  -- Return
    end tell
end tell
APPLESCRIPT
