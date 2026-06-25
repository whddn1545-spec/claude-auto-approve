#!/bin/bash
# Claude Auto-Approve.app 데스크탑 아이콘 생성

set -e
PROJ="$HOME/claude-auto-approve"
APP="$HOME/Desktop/Claude Auto-Approve.app"
PORT=17654

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Claude Auto-Approve.app 생성"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 기존 앱 제거
[ -d "$APP" ] && rm -rf "$APP"

# 앱 번들 구조
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# ── 1) 아이콘 생성 ─────────────────────────────────────────────
echo "→ 아이콘 생성 중..."
python3 "$PROJ/make_icon.py" "$APP/Contents/Resources"

# ── 2) 런처 스크립트 ───────────────────────────────────────────
cat > "$APP/Contents/MacOS/Claude Auto-Approve" << 'LAUNCHER'
#!/bin/bash
PROJ="$HOME/claude-auto-approve"
PORT=17654
CMUX_BIN="/Applications/cmux.app/Contents/Resources/bin/cmux"
CMUX_SOCK="$HOME/Library/Application Support/cmux/cmux.sock"

# 이미 실행 중이면 브라우저만 열기
if curl -s --max-time 1 "http://localhost:$PORT/" > /dev/null 2>&1; then
    open "http://localhost:$PORT"
    exit 0
fi

# ── 1순위: cmux가 실행 중이면 cmux 내부에서 실행 (소켓 인증 통과용) ──
if [ -S "$CMUX_SOCK" ] && pgrep -f "cmux.app/Contents/MacOS/cmux" > /dev/null 2>&1; then
    # cmux를 활성화하고 새 탭에서 서버 실행
    osascript <<'AS'
tell application "cmux" to activate
delay 0.6
tell application "System Events"
    tell process "cmux"
        -- 새 창 열기 (Cmd+N)
        keystroke "n" using {command down}
        delay 0.8
        -- 서버 시작 명령어 입력
        keystroke "python3 ~/claude-auto-approve/app.py"
        key code 36
    end tell
end tell
AS
    sleep 3
    open "http://localhost:$PORT"
    exit 0
fi

# ── 2순위: iTerm2 ──
ITERM_PATHS=(
    "/Applications/iTerm.app"
    "$HOME/Applications/iTerm.app"
    "/Applications/iTerm2.app"
)
USE_ITERM=false
for p in "${ITERM_PATHS[@]}"; do
    if [ -d "$p" ]; then
        USE_ITERM=true
        break
    fi
done

if $USE_ITERM; then
    osascript <<'AS'
tell application "iTerm2"
    activate
    set newWin to create window with default profile
    tell current session of newWin
        write text "cd ~/claude-auto-approve && python3 app.py"
    end tell
end tell
AS
else
    # ── 3순위: Terminal.app (cmux 미사용시) ──
    osascript <<AS
tell application "Terminal"
    activate
    do script "cd '$PROJ' && python3 app.py"
end tell
AS
fi
LAUNCHER

chmod +x "$APP/Contents/MacOS/Claude Auto-Approve"

# ── 3) Info.plist ─────────────────────────────────────────────
cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>Claude Auto-Approve</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIdentifier</key>
  <string>com.claude.autoapprove</string>
  <key>CFBundleName</key>
  <string>Claude Auto-Approve</string>
  <key>CFBundleDisplayName</key>
  <string>Claude Auto-Approve</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleVersion</key>
  <string>1.1</string>
  <key>CFBundleShortVersionString</key>
  <string>1.1</string>
  <key>LSMinimumSystemVersion</key>
  <string>11.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>LSUIElement</key>
  <false/>
</dict>
</plist>
PLIST

# ── 4) Finder에 앱으로 인식시키기 ────────────────────────────
touch "$APP"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 완료!"
echo ""
echo "  바탕화면에 'Claude Auto-Approve' 아이콘이 생성됐습니다."
echo "  더블클릭하면 앱이 시작됩니다."
echo ""
echo "  ※ 처음 실행 시 macOS 보안 경고가 뜰 수 있습니다:"
echo "    우클릭 → 열기 → 열기 (한 번만)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
