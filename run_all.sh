#!/usr/bin/env bash
# run_all.sh — 무인 빌드→리뷰 루프 오케스트레이터
#
# 지원서에 적은 흐름을 그대로 구현한다:
#   빌드 세션이 TODO를 구현 → 별도의 리뷰 세션이 diff를 검사하고 그 자리에서 고침
#   → 통과분을 커밋 → 다음 항목 → TODO가 전부 끝나면 PROGRESS.md 첫 줄에 ALL_DONE
#   → 사용량 한도에 걸리면 리셋까지 기다렸다가 재개
#
# 사용법:
#   bash run_all.sh <프로젝트경로>            # ALL_DONE까지 반복
#   bash run_all.sh <프로젝트경로> --once     # 빌드+리뷰 1사이클만 (첫 검증용)
#   bash run_all.sh <프로젝트경로> --dry-run  # 실제 호출 없이 계획만 출력
#
# 환경변수:
#   MAX_CYCLES=8     최대 사이클 수 (기본 8, 무한루프 방지)
#   MODEL=opus       사용할 모델 (미지정 시 Claude Code 기본값)

set -uo pipefail

# ─── 인자 ──────────────────────────────────────────────────────────────
PROJECT="${1:-}"
[ -z "$PROJECT" ] && { echo "사용법: bash run_all.sh <프로젝트경로> [--once|--dry-run]"; exit 1; }
PROJECT="$(cd "$PROJECT" 2>/dev/null && pwd)" || { echo "경로 없음: ${1}"; exit 1; }

ONCE=0; DRY=0
for a in "${@:2}"; do
  case "$a" in
    --once)    ONCE=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "모르는 옵션: $a"; exit 1 ;;
  esac
done

MAX_CYCLES="${MAX_CYCLES:-8}"
[ "$ONCE" = 1 ] && MAX_CYCLES=1

# ─── 사전 점검 ─────────────────────────────────────────────────────────
cd "$PROJECT" || exit 1
for f in META_PROMPT.md TODO.md TASTE.md CLAUDE.md PROGRESS.md; do
  [ -f "$f" ] || { echo "❌ $f 가 없다. 맥락 파일이 갖춰진 프로젝트에서만 돌아간다."; exit 1; }
done
git rev-parse --git-dir >/dev/null 2>&1 || { echo "❌ git 저장소가 아니다."; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "❌ claude CLI를 찾을 수 없다."; exit 1; }

RUN_ID="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="$PROJECT/.autorun/$RUN_ID"
mkdir -p "$LOG_DIR"
START_COMMIT="$(git rev-parse HEAD)"

# 되돌아갈 지점을 남긴다 — 무인으로 돌린 밤이 잘못됐을 때 여기로 리셋
echo "$START_COMMIT" > "$LOG_DIR/START_COMMIT"

CLAUDE_ARGS=(-p --permission-mode bypassPermissions --output-format json)
[ -n "${MODEL:-}" ] && CLAUDE_ARGS+=(--model "$MODEL")

# ─── 프롬프트 ──────────────────────────────────────────────────────────
read -r -d '' BUILD_PROMPT <<'EOF'
이 프로젝트 루트의 META_PROMPT.md 전문을 이 세션의 헌법으로 삼는다.
§0 세션 시작 의식부터 그대로 수행하라.

이번 세션의 범위:
- TODO.md의 현재 Phase에서 미완료 항목을 위에서부터 최대 __MAX_ITEMS__개까지만 구현한다.
- TASTE.md 루브릭의 '상'을 목표 수준으로 삼는다.
- 구현과 테스트를 함께 쓰고, 논리 단위마다 커밋한다.
- 판단이 애매하면 멈추지 말고 docs/DECISIONS.md에 §4 형식으로 기록하고 최선의 판단으로 진행한다.
- 3회 시도해도 막히면 PROGRESS.md의 BLOCKED에 재현 방법과 함께 적고 다음 항목으로 넘어간다.

품질 게이트는 이 세션이 아니라 뒤이어 뜨는 별도의 리뷰 세션이 수행한다.
너는 PROGRESS.md의 '완료'와 '다음 할 일'만 갱신하고 끝낸다.

TODO.md의 모든 항목이 완료되었다면, PROGRESS.md의 맨 첫 줄에 ALL_DONE 이라고만 적힌
줄을 추가하라. 남은 항목이 하나라도 있으면 절대 적지 마라.
EOF

read -r -d '' REVIEW_PROMPT <<'EOF'
너는 이 프로젝트의 까다로운 시니어 리뷰어다. 직전 빌드 세션과는 다른 사람이라고 생각하라.
구현자의 주장을 믿지 말고 직접 실행한 출력만 근거로 삼아라.

먼저 무엇이 바뀌었는지 확인한다:
  git log --oneline -12
  git diff HEAD~1 --stat   (필요하면 더 거슬러 올라가 이번 빌드분 전체를 본다)

그 다음 아래를 순서대로 수행하고, 각 단계에서 "실제로 실행한 명령과 그 출력"을 근거로 남겨라.

1. 전체 테스트를 실제로 실행한다. 실패가 있으면 원인을 고치고 다시 돌린다.
2. 하드코딩된 시크릿을 grep으로 색출한다. 발견 시 즉시 환경변수로 옮긴다.
3. CLAUDE.md의 '절대 하지 말 것' 목록을 이번 diff와 하나씩 대조한다.
4. TASTE.md 루브릭으로 이번 변경분을 항목별 상/중/하로 자가채점한다.
   '하'가 나온 항목은 그 자리에서 고친다. 이번 세션 범위 밖이라 미구현인 것은
   '하'가 아니라 '미착수'로 분류하고 그렇게 적어라.
5. META_PROMPT.md §5의 "직접 실행해 눈으로 확인 안 한 기능은 구현 안 된 것으로 간주한다"를
   이번 변경분에 적용하라. 새로 만든 화면·이미지·출력물이 있으면 실제로 실행하거나
   렌더해서 눈으로 확인한다. 확인하지 않았다면 통과시키지 마라.
6. 고친 것이 있으면 커밋한다.
7. PROGRESS.md의 '## 리뷰 로그' 섹션에 오늘 날짜로 각 단계 결과를 기록한다.
   통과한 것과 실패한 것을 모두 정직하게 적어라. 게이트를 통과하지 못한 채 끝내야 한다면
   PROGRESS.md 최상단에 ⚠️ FAILING 과 사유를 남겨라.

없는 문제를 지어내지 마라. 깨끗하면 깨끗하다고 적으면 된다.
EOF

# 한 세션에서 잡을 항목 수. META_PROMPT §2 의 상한이 3이므로 기본값 3.
# 첫 검증처럼 짧게 돌려보고 싶으면 MAX_ITEMS=1 로 줄인다.
MAX_ITEMS="${MAX_ITEMS:-3}"
case "$MAX_ITEMS" in (*[!0-9]*|'') echo "MAX_ITEMS 는 숫자여야 한다"; exit 1 ;; esac
BUILD_PROMPT="${BUILD_PROMPT//__MAX_ITEMS__/$MAX_ITEMS}"

# ─── 유틸 ──────────────────────────────────────────────────────────────
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG_DIR/run.log"; }

all_done() {
  [ -f PROGRESS.md ] && head -1 PROGRESS.md | grep -qx 'ALL_DONE'
}

# 출력에서 사용량 한도 신호를 찾는다 (2개 이상 일치해야 인정 — 오탐 방지)
is_rate_limited() {
  local t; t="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  local hits=0
  for p in "usage limit" "rate limit" "session limit" "you've hit" "resets" "quota"; do
    printf '%s' "$t" | grep -qF "$p" && hits=$((hits+1))
  done
  [ "$hits" -ge 2 ]
}

# 한도 메시지에서 실제로 기다려야 할 초를 계산한다.
#   상대 — "resets in 1h 5m" / "resets in 52m50s" / "resets in 30s"
#   절대 — "resets 8:40pm (Asia/Seoul)" / "resets 8pm" / "resets 20:40"
# 2026-09-11 실행에서 실제로 만난 것은 절대시각("session limit · resets 8:40pm")이었는데
# 상대시각만 읽고 있어서, 20분이면 될 것을 기본값 1시간으로 통째로 잤다. 둘 다 읽는다.
wait_seconds() {
  python3 - "$1" <<'PY'
import re, sys
from datetime import datetime, timedelta

t = sys.argv[1].lower()

m = re.search(r'resets?\s+in\s+((?:\d+\s*[hms]\s*)+)', t)
if m:
    span = m.group(1)
    def part(u):
        g = re.search(r'(\d+)\s*' + u, span)
        return int(g.group(1)) if g else 0
    total = part('h') * 3600 + part('m') * 60 + part('s')
    if total > 0:
        print(total + 30)          # 경계에서 또 튕기지 않게 30초 여유
        raise SystemExit

m = re.search(r'resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', t)
if m:
    hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or '')
    if ap == 'pm' and hh != 12:
        hh += 12
    elif ap == 'am' and hh == 12:
        hh = 0
    now = datetime.now()
    tgt = now.replace(hour=hh % 24, minute=mm, second=30, microsecond=0)
    if tgt <= now:
        # 리셋 시각이 이미 지났다. 다음 날로 넘기면 24시간을 자게 되어
        # 밤샘 한 번이 통째로 날아간다. 이미 풀렸다고 보고 곧바로 재시도한다.
        print(60)
        raise SystemExit
    delta = int((tgt - now).total_seconds())
    # 세션 한도는 최대 5시간짜리다. 그보다 멀면 am/pm 오독일 가능성이 높으니
    # 무작정 믿고 오래 자지 않는다.
    print(min(delta, 6 * 3600))
    raise SystemExit

print(900)                          # 못 읽으면 15분 뒤 재시도
PY
}

# 한 세션 실행. $1=라벨 $2=프롬프트 → 성공 0 / 한도 2 / 실패 1
run_session() {
  local label="$1" prompt="$2"
  local out="$LOG_DIR/${label}.json"

  if [ "$DRY" = 1 ]; then
    log "🔍 [dry-run] $label 세션을 여기서 호출한다:"
    printf '  claude %s\n' "${CLAUDE_ARGS[*]}"
    printf '  --- 프롬프트 앞 3줄 ---\n%s\n  ---\n' "$(printf '%s' "$prompt" | head -3 | sed 's/^/  /')"
    return 0
  fi

  log "▶ $label 세션 시작"
  # --output-format json 은 세션이 끝나야 한 번에 뱉는다. 그동안 로그가 완전히
  # 조용해서 멈춘 것처럼 보이므로, 2분마다 생존 신호를 남긴다.
  local body rc ticker t0=$SECONDS
  ( while :; do sleep 120; log "   … $label 진행 중 ($(( (SECONDS - t0) / 60 ))분)"; done ) &
  ticker=$!
  body="$(printf '%s' "$prompt" | claude "${CLAUDE_ARGS[@]}" 2>&1)"
  rc=$?
  kill "$ticker" 2>/dev/null; wait "$ticker" 2>/dev/null
  printf '%s' "$body" > "$out"

  if is_rate_limited "$body"; then
    log "⏳ 사용량 한도 감지"
    return 2
  fi
  if [ "$rc" -ne 0 ]; then
    log "❌ $label 실패 (exit=$rc). 로그: $out"
    return 1
  fi
  log "✅ $label 완료 → $out"
  return 0
}

# 한도면 리셋까지 기다렸다 다시 시도한다. 총 MAX_TRIES 회 "시도"한다.
# (이전 버전은 마지막 실패 뒤에도 한 시간을 자고 나서 재시도 없이 포기했다 —
#  그 잠은 통째로 낭비였다. 이제 시도와 시도 사이에만 잔다.)
run_with_backoff() {
  local label="$1" prompt="$2" tries="${MAX_TRIES:-4}" i=1 w
  while :; do
    run_session "$label" "$prompt"
    case $? in
      0) return 0 ;;
      2) : ;;            # 한도 — 아래에서 대기 후 재시도
      *) return 1 ;;
    esac
    if [ "$i" -ge "$tries" ]; then
      log "❌ $label — 한도로 ${tries}회 실패. 중단한다."
      return 1
    fi
    w="$(wait_seconds "$(cat "$LOG_DIR/${label}.json" 2>/dev/null)")"
    log "😴 ${w}초 대기 후 재시도 ($((i+1))/${tries})"
    sleep "$w"
    i=$((i+1))
  done
}

# ─── 메인 루프 ─────────────────────────────────────────────────────────
log "════════════════════════════════════════════"
log "프로젝트: $PROJECT"
log "시작 커밋: ${START_COMMIT:0:8}   로그: $LOG_DIR"
log "최대 사이클: $MAX_CYCLES$([ "$DRY" = 1 ] && echo '  (DRY RUN)')"
log "════════════════════════════════════════════"

if all_done; then
  log "🏁 PROGRESS.md 첫 줄이 이미 ALL_DONE 이다. 할 일이 없다."
  exit 0
fi

cycle=1
while [ "$cycle" -le "$MAX_CYCLES" ]; do
  log "──────── 사이클 $cycle/$MAX_CYCLES ────────"
  before="$(git rev-parse HEAD)"

  run_with_backoff "cycle${cycle}-build" "$BUILD_PROMPT" || { log "루프 중단 (빌드 실패)"; break; }
  run_with_backoff "cycle${cycle}-review" "$REVIEW_PROMPT" || { log "루프 중단 (리뷰 실패)"; break; }

  after="$(git rev-parse HEAD)"
  if [ "$DRY" = 0 ]; then
    n="$(git rev-list --count "${before}..${after}" 2>/dev/null || echo 0)"
    log "📦 이번 사이클 커밋 $n건"
    if [ "$n" = "0" ]; then
      log "⚠️ 커밋이 하나도 없다. 더 진행해도 같은 상태일 가능성이 높아 중단한다."
      break
    fi
  fi

  if all_done; then
    log "🏁 ALL_DONE 감지 — TODO 소진"
    break
  fi
  cycle=$((cycle+1))
done

# ─── 마무리 ────────────────────────────────────────────────────────────
END_COMMIT="$(git rev-parse HEAD)"
TOTAL="$(git rev-list --count "${START_COMMIT}..${END_COMMIT}" 2>/dev/null || echo 0)"
log "════════════════════════════════════════════"
log "총 커밋 $TOTAL건   ${START_COMMIT:0:8} → ${END_COMMIT:0:8}"
log "되돌리려면:  git -C \"$PROJECT\" reset --hard $START_COMMIT"
log "로그:        $LOG_DIR"
log "════════════════════════════════════════════"
