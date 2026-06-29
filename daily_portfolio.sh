#!/bin/bash
# 일일 포트폴리오 생성 트리거 — launchd에서 매일 밤 11시 호출
# 앱이 실행 중이면 HTTP로 트리거, 아니면 직접 실행

if lsof -ti :17654 &>/dev/null; then
    curl -s -X POST http://localhost:17654/api/daily-portfolio
else
    # 앱이 꺼져있으면 직접 Python으로 실행
    cd "$(dirname "$0")"
    python3 - <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import run_daily_portfolio, _find_active_git_repos
print(f"[daily] 직접 실행 모드 — {len(_find_active_git_repos(24))}개 레포 처리")
run_daily_portfolio()
import time; time.sleep(180)  # API 호출 완료 대기
PYEOF
fi
