#!/bin/bash
# Claude Auto-Approve 설치 스크립트
# tesseract/tkinter 불필요 — AppleScript + Pillow 만 사용

set -e
cd "$(dirname "$0")"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Claude Auto-Approve 설치"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Pillow 확인
python3 -c "import PIL" 2>/dev/null && echo "✅ Pillow 설치됨" || {
    echo "→ Pillow 설치 중..."
    pip3 install --user Pillow
}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 준비 완료!"
echo ""
echo "  실행: python3 app.py"
echo "  또는: bash run.sh"
echo ""
echo "  ⚠️  macOS 권한 2가지가 필요합니다:"
echo "  1) 화면 기록 (Screen Recording)"
echo "     시스템 설정 → 개인정보 보호 및 보안 → 화면 기록"
echo "     → 터미널 앱 허용"
echo ""
echo "  2) 손쉬운 사용 (Accessibility)"
echo "     시스템 설정 → 개인정보 보호 및 보안 → 손쉬운 사용"
echo "     → 터미널 앱 허용"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
