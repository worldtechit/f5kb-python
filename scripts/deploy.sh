#!/usr/bin/env bash
# deploy.sh — validate SSM params, sam build, sam deploy, record to S3.
#
# Usage:
#   bash scripts/deploy.sh staging
#   bash scripts/deploy.sh prod
#
# Prerequisites:
#   aws CLI authenticated, sam CLI installed, uv installed.
#   SSM SecureStrings pre-created (this script checks before deploying):
#     /f5kb/<stage>/slack/webhook-url
#     /f5kb/<stage>/slack/signing-secret
#     /f5kb/<stage>/github/token   (optional — raises F5_GitHub rate limit)
#
# After a successful deploy the script uploads a config snapshot to:
#   s3://f5kb-articles-<account>-<stage>/deployments/<stage>/

set -euo pipefail

STAGE="${1:-}"
if [[ "$STAGE" != "staging" && "$STAGE" != "prod" ]]; then
    echo "Usage: bash scripts/deploy.sh [staging|prod]" >&2
    exit 1
fi

REGION="us-east-2"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
BUCKET="f5kb-articles-${ACCOUNT}-${STAGE}"

echo "==> f5kb deploy  stage=${STAGE}  account=${ACCOUNT}  region=${REGION}"

# ── 1. Validate required SSM params ──────────────────────────────────────────

REQUIRED_PARAMS=(
    "/f5kb/${STAGE}/slack/webhook-url"
    "/f5kb/${STAGE}/slack/signing-secret"
)
OPTIONAL_PARAMS=(
    "/f5kb/${STAGE}/github/token"
)

echo ""
echo "==> Checking SSM parameters..."
MISSING=0
for param in "${REQUIRED_PARAMS[@]}"; do
    if aws ssm get-parameter --name "$param" --region "$REGION" \
           --query Parameter.Name --output text >/dev/null 2>&1; then
        echo "    [ok]  $param"
    else
        echo "    [MISSING]  $param  ← required" >&2
        MISSING=1
    fi
done
for param in "${OPTIONAL_PARAMS[@]}"; do
    if aws ssm get-parameter --name "$param" --region "$REGION" \
           --query Parameter.Name --output text >/dev/null 2>&1; then
        echo "    [ok]  $param  (optional)"
    else
        echo "    [--]  $param  (optional, not set)"
    fi
done

if [[ "$MISSING" -ne 0 ]]; then
    echo "" >&2
    echo "ERROR: Missing required SSM parameters. Create them first:" >&2
    echo "  aws ssm put-parameter --name '/f5kb/${STAGE}/slack/webhook-url' \\" >&2
    echo "      --type SecureString --value '...' --region ${REGION}" >&2
    echo "  aws ssm put-parameter --name '/f5kb/${STAGE}/slack/signing-secret' \\" >&2
    echo "      --type SecureString --value '...' --region ${REGION}" >&2
    exit 1
fi

# ── 2. Build ──────────────────────────────────────────────────────────────────

echo ""
echo "==> sam build..."
sam build

# ── 3. Deploy ─────────────────────────────────────────────────────────────────

echo ""
echo "==> sam deploy --config-env ${STAGE}..."
sam deploy --config-env "${STAGE}"

# ── 4. Record deploy to S3 ────────────────────────────────────────────────────

echo ""
echo "==> Recording deploy to s3://${BUCKET}/deployments/${STAGE}/..."
uv run python scripts/record_deploy.py \
    --stage   "$STAGE" \
    --bucket  "$BUCKET" \
    --region  "$REGION"

echo ""
echo "==> Done."
echo "    Next: make sync-config BUCKET=${BUCKET}"
