#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/infra/deploy.conf"

ALICLOUD_ONLY=0
CF_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --alicloud-only) ALICLOUD_ONLY=1 ;;
    --cf-only) CF_ONLY=1 ;;
  esac
done

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[sizzle]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
die()  { echo -e "${RED}  ✗${NC} $*" >&2; exit 1; }

# ── Pre-flight checks ─────────────────────────────────────────
log "Pre-flight checks"

command -v aliyun  >/dev/null || die "aliyun CLI not found. Install: brew install aliyun-cli"
command -v docker  >/dev/null || die "docker not found."
command -v wrangler >/dev/null || die "wrangler not found. Run: npm install"

aliyun sts GetCallerIdentity --region "$ALICLOUD_REGION" >/dev/null 2>&1 \
  || die "aliyun CLI not authenticated. Run: aliyun configure"
ok "Alibaba Cloud credentials valid"

if [[ -f "$ROOT/.dev.vars" ]]; then
  set -a; source "$ROOT/.dev.vars"; set +a
  ok "Loaded .dev.vars"
else
  die ".dev.vars not found"
fi

if [[ -z "${EDGE_API_TOKEN:-}" ]]; then
  EDGE_API_TOKEN="$(openssl rand -hex 32)"
  echo "" >> "$ROOT/.dev.vars"
  echo "EDGE_API_TOKEN=$EDGE_API_TOKEN" >> "$ROOT/.dev.vars"
  ok "Generated EDGE_API_TOKEN"
fi

SIZZLE_FC_ENDPOINT="https://${ALICLOUD_ACCOUNT_ID}.${ALICLOUD_REGION}.fc.aliyuncs.com"

# FC env vars — use SIZZLE_ prefix to avoid FC reserved prefix
FC_ENV_VARS=$(python3 -c "
import json, os
print(json.dumps({
    'DASHSCOPE_API_KEY': os.environ['DASHSCOPE_API_KEY'],
    'ALIBABA_CLOUD_ACCESS_KEY_ID': os.environ['ALIBABA_CLOUD_ACCESS_KEY_ID'],
    'ALIBABA_CLOUD_ACCESS_KEY_SECRET': os.environ['ALIBABA_CLOUD_ACCESS_KEY_SECRET'],
    'ALIBABA_CLOUD_REGION': '$ALICLOUD_REGION',
    'OSS_ENDPOINT': '$OSS_ENDPOINT',
    'OSS_BUCKET': '$OSS_BUCKET',
    'OSS_ACCESS_KEY_ID': os.environ['ALIBABA_CLOUD_ACCESS_KEY_ID'],
    'OSS_ACCESS_KEY_SECRET': os.environ['ALIBABA_CLOUD_ACCESS_KEY_SECRET'],
    'SIZZLE_FC_ENDPOINT': '$SIZZLE_FC_ENDPOINT',
    'SIZZLE_FC_PIPELINE_FUNCTION': '$FC_FUNCTION_PIPELINE',
    'EDGE_API_TOKEN': os.environ['EDGE_API_TOKEN'],
    'GITHUB_APP_ID': os.environ.get('GITHUB_APP_ID', ''),
    'GITHUB_APP_PRIVATE_KEY': os.environ.get('GITHUB_APP_PRIVATE_KEY', ''),
}, separators=(',',':')))
")

if [[ "$CF_ONLY" -eq 1 ]]; then
  log "Skipping Alibaba Cloud (--cf-only)"
else

# ── 1. OSS Bucket ─────────────────────────────────────────────
log "Checking OSS bucket: $OSS_BUCKET"
if aliyun oss ls "oss://$OSS_BUCKET" --endpoint "oss-${ALICLOUD_REGION}.aliyuncs.com" >/dev/null 2>&1; then
  ok "Bucket exists"
else
  aliyun oss mb "oss://$OSS_BUCKET" \
    --endpoint "oss-${ALICLOUD_REGION}.aliyuncs.com" \
    --acl private 2>&1 \
    || die "Failed to create OSS bucket."
  ok "Created $OSS_BUCKET"
fi

# ── 2. API Function (Python 3.10 code zip) ────────────────────
log "Packaging API function"
API_ZIP="/tmp/sizzle-api-$$.zip"
DEPS_DIR=$(mktemp -d)
uv pip install --target "$DEPS_DIR" "oss2>=2.19.1" --quiet 2>&1

# Remove native packages that conflict with FC runtime
rm -rf "$DEPS_DIR"/cffi "$DEPS_DIR"/cffi-* "$DEPS_DIR"/_cffi_backend* \
       "$DEPS_DIR"/cryptography "$DEPS_DIR"/cryptography-* \
       "$DEPS_DIR"/Crypto "$DEPS_DIR"/pycryptodome-* \
       "$DEPS_DIR"/bin
find "$DEPS_DIR" -name "*.so" -delete 2>/dev/null
find "$DEPS_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

rm -f "$API_ZIP"
(cd "$DEPS_DIR" && zip -q -r "$API_ZIP" .)
python3 -c "
import zipfile
z = zipfile.ZipFile('$API_ZIP', 'a', zipfile.ZIP_DEFLATED)
z.write('$ROOT/infra/alicloud/fc3_entry.py', 'index.py')
z.close()
"
rm -rf "$DEPS_DIR"
ok "API zip built ($(du -h "$API_ZIP" | cut -f1))"

log "Uploading API code to OSS"
aliyun oss cp "$API_ZIP" \
  "oss://${OSS_BUCKET}/deploy/sizzle-api.zip" \
  --endpoint "oss-${ALICLOUD_REGION}.aliyuncs.com" --force >/dev/null 2>&1
rm -f "$API_ZIP"
ok "Uploaded"

log "Deploying FC function: $FC_FUNCTION_API"
if aliyun fc get-function --region "$ALICLOUD_REGION" --function-name "$FC_FUNCTION_API" >/dev/null 2>&1; then
  aliyun fc update-function \
    --region "$ALICLOUD_REGION" \
    --function-name "$FC_FUNCTION_API" \
    --handler "index.handler" \
    --runtime "python3.10" \
    --cpu "$FC_API_CPU" \
    --memory-size "$FC_API_MEMORY" \
    --disk-size 512 \
    --timeout "$FC_API_TIMEOUT" \
    --environment-variables "$FC_ENV_VARS" \
    --code '{"ossBucketName":"'"${OSS_BUCKET}"'","ossObjectName":"deploy/sizzle-api.zip"}' \
    >/dev/null 2>&1
  ok "Updated $FC_FUNCTION_API"
else
  aliyun fc create-function \
    --region "$ALICLOUD_REGION" \
    --function-name "$FC_FUNCTION_API" \
    --handler "index.handler" \
    --runtime "python3.10" \
    --cpu "$FC_API_CPU" \
    --memory-size "$FC_API_MEMORY" \
    --disk-size 512 \
    --timeout "$FC_API_TIMEOUT" \
    --environment-variables "$FC_ENV_VARS" \
    --code '{"ossBucketName":"'"${OSS_BUCKET}"'","ossObjectName":"deploy/sizzle-api.zip"}' \
    --description "Sizzle video AI — HTTP API" \
    >/dev/null 2>&1
  ok "Created $FC_FUNCTION_API"

  # Create HTTP trigger
  aliyun fc create-trigger \
    --region "$ALICLOUD_REGION" \
    --function-name "$FC_FUNCTION_API" \
    --trigger-name "http-trigger" \
    --trigger-type "http" \
    --trigger-config '{"methods":["GET","POST","OPTIONS"],"authType":"anonymous"}' \
    >/dev/null 2>&1
  ok "HTTP trigger created"
fi

# ── 3. Container Image (ghcr.io) ──────────────────────────────
CR_IMAGE="${CR_REGISTRY}/${CR_NAMESPACE}/${CR_REPO}:${CR_IMAGE_TAG}"
log "Building container image: $CR_IMAGE"

if ! docker info >/dev/null 2>&1; then
  warn "Docker not running — skipping worker function deployment"
else
  docker build \
    --platform linux/amd64 \
    -t "$CR_IMAGE" \
    -f "$ROOT/infra/alicloud/Dockerfile" \
    "$ROOT" 2>&1 | tail -3
  ok "Image built"

  GH_TOKEN=$(gh auth token 2>/dev/null || echo "")
  if [[ -n "$GH_TOKEN" ]]; then
    log "Pushing to GitHub Container Registry"
    echo "$GH_TOKEN" | docker login ghcr.io --username ammbo --password-stdin >/dev/null 2>&1
    docker push "$CR_IMAGE" 2>&1 | tail -3
    ok "Image pushed"

    # ── 4. Worker Functions (custom container) ─────────────────
    CONTAINER_CONFIG='{"image":"'"${CR_IMAGE}"'","port":9000,"registryConfig":{"authConfig":{"userName":"ammbo","password":"'"${GH_TOKEN}"'"}}}'

    for FUNC_NAME in "$FC_FUNCTION_PIPELINE" "$FC_FUNCTION_RENDER"; do
      case "$FUNC_NAME" in
        *pipeline*) FUNC_CPU=$FC_PIPELINE_CPU; FUNC_MEM=$FC_PIPELINE_MEMORY; FUNC_TO=$FC_PIPELINE_TIMEOUT; FUNC_DISK=$FC_PIPELINE_DISK; HANDLER="pipeline_worker.handler" ;;
        *render*)   FUNC_CPU=$FC_RENDER_CPU;   FUNC_MEM=$FC_RENDER_MEMORY;   FUNC_TO=$FC_RENDER_TIMEOUT;   FUNC_DISK=512; HANDLER="render_worker.handler" ;;
      esac

      log "Deploying FC function: $FUNC_NAME"
      if aliyun fc get-function --region "$ALICLOUD_REGION" --function-name "$FUNC_NAME" >/dev/null 2>&1; then
        aliyun fc update-function \
          --region "$ALICLOUD_REGION" \
          --function-name "$FUNC_NAME" \
          --handler "$HANDLER" \
          --cpu "$FUNC_CPU" \
          --memory-size "$FUNC_MEM" \
          --disk-size "$FUNC_DISK" \
          --timeout "$FUNC_TO" \
          --environment-variables "$FC_ENV_VARS" \
          --custom-container-config "$CONTAINER_CONFIG" \
          >/dev/null 2>&1
        ok "Updated $FUNC_NAME"
      else
        aliyun fc create-function \
          --region "$ALICLOUD_REGION" \
          --function-name "$FUNC_NAME" \
          --runtime "custom-container" \
          --handler "$HANDLER" \
          --cpu "$FUNC_CPU" \
          --memory-size "$FUNC_MEM" \
          --disk-size "$FUNC_DISK" \
          --timeout "$FUNC_TO" \
          --environment-variables "$FC_ENV_VARS" \
          --custom-container-config "$CONTAINER_CONFIG" \
          --description "Sizzle video AI — $FUNC_NAME" \
          >/dev/null 2>&1
        ok "Created $FUNC_NAME"
      fi
    done

  else
    warn "gh CLI not authenticated — skipping worker function deployment"
  fi
fi

# Get the API URL for Cloudflare config
API_URL=$(aliyun fc get-function \
  --region "$ALICLOUD_REGION" \
  --function-name "$FC_FUNCTION_API" 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('urlInternet',''))" 2>/dev/null || echo "")

if [[ -z "$API_URL" ]]; then
  API_URL=$(aliyun fc list-triggers \
    --region "$ALICLOUD_REGION" \
    --function-name "$FC_FUNCTION_API" 2>/dev/null \
    | python3 -c "import sys,json; ts=json.load(sys.stdin).get('triggers',[]); print(ts[0]['httpTrigger']['urlInternet'] if ts else '')" 2>/dev/null || echo "")
fi
ok "Backend URL: ${API_URL:-unknown}"

fi # end of Alibaba Cloud section

# ── 5. Cloudflare Worker ──────────────────────────────────────
if [[ "$ALICLOUD_ONLY" -eq 0 ]]; then
  log "Deploying Cloudflare Worker"

  if [[ -n "${API_URL:-}" ]]; then
    echo "$API_URL" | wrangler secret put ALIBABA_BACKEND_URL >/dev/null 2>&1 \
      || warn "Could not set ALIBABA_BACKEND_URL secret"
  fi

  echo "$EDGE_API_TOKEN" | wrangler secret put ALIBABA_BACKEND_TOKEN >/dev/null 2>&1 \
    || warn "Could not set ALIBABA_BACKEND_TOKEN secret"

  ok "Cloudflare secrets set"

  wrangler deploy 2>&1 | tail -5
  ok "Cloudflare Worker deployed"
else
  log "Skipping Cloudflare (--alicloud-only)"
fi

# ── Done ───────────────────────────────────────────────────────
echo ""
log "Deployment complete!"
echo ""
echo -e "  ${CYAN}Web:${NC}      https://sizzlevideoai.com"
echo -e "  ${CYAN}Health:${NC}   https://sizzlevideoai.com/api/health"
if [[ -n "${API_URL:-}" ]]; then
  echo -e "  ${CYAN}Backend:${NC}  $API_URL"
fi
echo ""
