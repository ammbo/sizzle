#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/infra/deploy.conf"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[sizzle-setup]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
fail() { echo -e "${RED}  ✗${NC} $*"; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  Sizzle — First-Time Setup Checklist         ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
echo ""

PASS=0
TOTAL=0

check() {
  local label="$1"
  TOTAL=$((TOTAL + 1))
  shift
  if "$@" >/dev/null 2>&1; then
    ok "$label"
    PASS=$((PASS + 1))
    return 0
  else
    fail "$label"
    return 1
  fi
}

# ── CLI tools ─────────────────────────────────────────────────
log "CLI tools"
check "aliyun CLI installed" command -v aliyun
check "docker installed" command -v docker
check "wrangler installed" command -v wrangler

# ── Alibaba Cloud auth ────────────────────────────────────────
log "Alibaba Cloud credentials"
check "aliyun CLI authenticated" aliyun sts GetCallerIdentity --region "$ALICLOUD_REGION"

# ── Secrets file ──────────────────────────────────────────────
log "Secrets (.dev.vars)"
if [[ -f "$ROOT/.dev.vars" ]]; then
  set -a; source "$ROOT/.dev.vars" 2>/dev/null; set +a
fi
check "DASHSCOPE_API_KEY set" test -n "${DASHSCOPE_API_KEY:-}"
check "ALIBABA_CLOUD_ACCESS_KEY_ID set" test -n "${ALIBABA_CLOUD_ACCESS_KEY_ID:-}"
check "ALIBABA_CLOUD_ACCESS_KEY_SECRET set" test -n "${ALIBABA_CLOUD_ACCESS_KEY_SECRET:-}"

# ── Alibaba Cloud services ────────────────────────────────────
log "Alibaba Cloud services (require console activation)"

TOTAL=$((TOTAL + 1))
if aliyun oss ls --endpoint "oss-${ALICLOUD_REGION}.aliyuncs.com" >/dev/null 2>&1 && \
   ! aliyun oss ls --endpoint "oss-${ALICLOUD_REGION}.aliyuncs.com" 2>&1 | grep -q "UserDisable"; then
  ok "OSS activated"
  PASS=$((PASS + 1))
else
  fail "OSS not activated → https://oss.console.aliyun.com/"
fi

TOTAL=$((TOTAL + 1))
if aliyun fc ListFunctions --region "$ALICLOUD_REGION" --limit 1 >/dev/null 2>&1; then
  ok "Function Compute activated"
  PASS=$((PASS + 1))
else
  fail "Function Compute not activated → https://fc.console.aliyun.com/"
fi

TOTAL=$((TOTAL + 1))
if docker login "$CR_REGISTRY" --username "${ALIBABA_CLOUD_ACCESS_KEY_ID:-}" --password "${CR_PASSWORD:-nopass}" >/dev/null 2>&1; then
  ok "Container Registry accessible"
  PASS=$((PASS + 1))
else
  fail "Container Registry — set password at https://cr.console.aliyun.com/ → Settings → Set Password"
  warn "  Then add CR_PASSWORD=<your-password> to .dev.vars"
fi

# ── OSS bucket ────────────────────────────────────────────────
log "OSS bucket ($OSS_BUCKET)"
TOTAL=$((TOTAL + 1))
if aliyun oss ls "oss://$OSS_BUCKET" --endpoint "oss-${ALICLOUD_REGION}.aliyuncs.com" >/dev/null 2>&1; then
  ok "Bucket exists"
  PASS=$((PASS + 1))
else
  fail "Bucket not created — will be created on first deploy"
fi

# ── Cloudflare ────────────────────────────────────────────────
log "Cloudflare"
TOTAL=$((TOTAL + 1))
if wrangler whoami >/dev/null 2>&1; then
  ok "Wrangler authenticated"
  PASS=$((PASS + 1))
else
  fail "Wrangler not authenticated — run: wrangler login"
fi

# ── Summary ───────────────────────────────────────────────────
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
if [[ $PASS -eq $TOTAL ]]; then
  echo -e "${GREEN}All $TOTAL checks passed. Run: npm run deploy${NC}"
else
  echo -e "${YELLOW}$PASS/$TOTAL checks passed.${NC}"
  echo ""
  echo "Fix the failing checks above, then run: npm run setup"
  echo "Once all checks pass: npm run deploy"
fi
echo ""
