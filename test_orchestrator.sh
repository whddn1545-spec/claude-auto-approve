#!/usr/bin/env bash
# run_all.sh 오케스트레이션 회귀 테스트.
#
# 진짜 claude 를 부르면 토큰이 들고 몇십 분이 걸리며 한도 경로는 재현조차 어렵다.
# 그래서 PATH 앞에 가짜 claude 를 꽂고 루프 로직만 검증한다.
# 이 하네스가 2026-09-11 에 잡은 버그:
#   - 하트비트 서브셸이 `sleep 120` 안에 있으면 kill 이 안 먹어 세션마다 2분씩 샜다
#   - `$n건` 처럼 변수 뒤에 한글이 붙어 set -u 에서 unbound variable 로 죽었다
#
#   bash test_orchestrator.sh

set -uo pipefail

RUNNER="$(cd "$(dirname "$0")" && pwd)/run_all.sh"
[ -f "$RUNNER" ] || { echo "run_all.sh 를 찾을 수 없다: $RUNNER"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; PROJ="$TMP/proj"
mkdir -p "$BIN" "$PROJ"

# ─── 가짜 claude ────────────────────────────────────────────────────────
cat > "$BIN/claude" <<STUB
#!/usr/bin/env bash
P=\$(cat)
R="$PROJ"
N=\$(cat "$TMP/turn" 2>/dev/null || echo 0); N=\$((N+1)); echo \$N > "$TMP/turn"

if [ -n "\${FAKE_RATELIMIT:-}" ] && [ "\$N" -le "\$FAKE_RATELIMIT" ]; then
  echo '{"type":"result","is_error":true,"result":"You'"'"'ve hit your session limit · resets in 2s"}'
  exit 0
fi

if printf '%s' "\$P" | grep -q "시니어 리뷰어"; then
  echo "- G1 테스트 통과 / G4 '하' 없음 (턴 \$N)" >> "\$R/PROGRESS.md"
  git -C "\$R" add -A >/dev/null 2>&1
  git -C "\$R" commit -qm "리뷰: 게이트 5종 통과 (턴 \$N)" >/dev/null 2>&1
  echo '{"type":"result","is_error":false,"result":"리뷰 완료"}'
else
  echo "work \$N" > "\$R/feature_\$N.txt"
  if [ "\$N" -ge "\${ALLDONE_AT:-99}" ]; then
    printf 'ALL_DONE\n%s\n' "\$(cat "\$R/PROGRESS.md")" > "\$R/P.t" && mv "\$R/P.t" "\$R/PROGRESS.md"
  fi
  git -C "\$R" add -A >/dev/null 2>&1
  git -C "\$R" commit -qm "빌드: 항목 구현 (턴 \$N)" >/dev/null 2>&1
  echo '{"type":"result","is_error":false,"result":"구현 완료"}'
fi
STUB
chmod +x "$BIN/claude"
export PATH="$BIN:$PATH"

reset_proj() {
  rm -rf "$PROJ" "$TMP/turn"; mkdir -p "$PROJ"
  cd "$PROJ" || exit 1
  git init -q .
  git config user.email t@local; git config user.name test; git config commit.gpgsign false
  for f in META_PROMPT.md TODO.md TASTE.md CLAUDE.md; do echo "# $f" > "$f"; done
  echo "# PROGRESS" > PROGRESS.md
  git add -A && git commit -qm init >/dev/null
}

PASS=0; FAIL=0
check() { # $1=설명 $2=조건결과(0/1)
  if [ "$2" = 0 ]; then echo "✅ $1"; PASS=$((PASS+1))
  else echo "❌ $1"; FAIL=$((FAIL+1)); fi
}

# ─── A. 빌드 → 리뷰 한 사이클 ───────────────────────────────────────────
echo "── A. 빌드 → 리뷰 한 사이클"
reset_proj
OUT="$(bash "$RUNNER" "$PROJ" --once 2>&1)"
grep -q "cycle1-build 세션 시작"  <<<"$OUT"; check "빌드 세션이 떴다" $?
grep -q "cycle1-review 세션 시작" <<<"$OUT"; check "리뷰 세션이 따로 떴다" $?
grep -q "이번 사이클 커밋 2건"      <<<"$OUT"; check "커밋 2건(빌드+리뷰)을 셌다" $?
grep -q "unbound variable"        <<<"$OUT"; [ $? -ne 0 ]; check "unbound variable 없음" $?
grep -q "되돌리려면"               <<<"$OUT"; check "되돌릴 지점을 안내했다" $?

# ─── B. ALL_DONE 까지 반복 ──────────────────────────────────────────────
echo; echo "── B. ALL_DONE 감지"
reset_proj
OUT="$(ALLDONE_AT=5 MAX_CYCLES=6 bash "$RUNNER" "$PROJ" 2>&1)"
grep -q "ALL_DONE 감지" <<<"$OUT"; check "ALL_DONE 을 감지해 멈췄다" $?
[ "$(grep -c '──────── 사이클' <<<"$OUT")" = 3 ]; check "6회 상한 전에 3사이클로 끝났다" $?

# 이미 ALL_DONE 인 프로젝트는 즉시 종료
OUT="$(bash "$RUNNER" "$PROJ" --once 2>&1)"
grep -q "이미 ALL_DONE" <<<"$OUT"; check "이미 끝난 프로젝트는 바로 종료" $?

# ─── C. 한도 → 대기 → 재시도 ────────────────────────────────────────────
echo; echo "── C. 사용량 한도 대기 후 재개"
reset_proj
OUT="$(FAKE_RATELIMIT=2 bash "$RUNNER" "$PROJ" --once 2>&1)"
[ "$(grep -c '사용량 한도 감지' <<<"$OUT")" = 2 ]; check "한도를 두 번 감지했다" $?
grep -q "32초 대기 후 재시도"  <<<"$OUT"; check "리셋 시각을 읽어 32초만 기다렸다" $?
grep -q "cycle1-review 세션 시작" <<<"$OUT"; check "재개 후 리뷰까지 완주했다" $?

# ─── D. 가드 ────────────────────────────────────────────────────────────
echo; echo "── D. 가드"
bash "$RUNNER" >/dev/null 2>&1;                     [ $? -ne 0 ]; check "인자 없으면 거부" $?
bash "$RUNNER" "$TMP" >/dev/null 2>&1;              [ $? -ne 0 ]; check "맥락 파일 없으면 거부" $?
MAX_ITEMS=abc bash "$RUNNER" "$PROJ" >/dev/null 2>&1; [ $? -ne 0 ]; check "MAX_ITEMS 숫자 아니면 거부" $?

echo
echo "══════════════════════════════════════════"
echo "  통과 $PASS / 실패 $FAIL"
echo "══════════════════════════════════════════"
[ "$FAIL" = 0 ] || exit 1
