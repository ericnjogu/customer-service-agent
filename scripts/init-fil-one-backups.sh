#!/usr/bin/env bash
set -euo pipefail

: "${FIL_ONE_ACCESS_KEY_ID:?Set FIL_ONE_ACCESS_KEY_ID}"
: "${FIL_ONE_SECRET_ACCESS_KEY:?Set FIL_ONE_SECRET_ACCESS_KEY}"
: "${RESTIC_PASSWORD:?Set RESTIC_PASSWORD}"

export AWS_ACCESS_KEY_ID="$FIL_ONE_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$FIL_ONE_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION=eu-west-1
export AWS_S3_ADDRESSING_STYLE=path
export RESTIC_REPOSITORY=s3:https://eu-west-1.s3.filonecontent.com/ristoh-css-postgres/production/cluster-state

aws s3api head-bucket \
  --bucket ristoh-css-postgres \
  --endpoint-url https://eu-west-1.s3.filonecontent.com

if restic snapshots >/dev/null 2>&1; then
  echo "Encrypted cluster-state repository already initialized"
else
  restic init
  echo "Initialized encrypted cluster-state repository"
fi
