#!/usr/bin/env bash
set -euo pipefail

: "${STORAGE_BOX_HOST:?Set STORAGE_BOX_HOST}"
: "${STORAGE_BOX_USER:?Set STORAGE_BOX_USER}"
: "${STORAGE_BOX_SSH_KEY:?Set STORAGE_BOX_SSH_KEY}"
: "${STORAGE_BOX_KNOWN_HOSTS:?Set STORAGE_BOX_KNOWN_HOSTS}"
: "${BORG_PASSPHRASE:?Set BORG_PASSPHRASE}"

export BORG_RSH="ssh -i ${STORAGE_BOX_SSH_KEY} -o BatchMode=yes -o UserKnownHostsFile=${STORAGE_BOX_KNOWN_HOSTS}"

for repository in cluster-state postgres-base postgres-wal; do
  export BORG_REPO="ssh://${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}:23/./ristoh-production/${repository}"
  borg check --repository-only
  borg list --last 3
done

kubectl -n customer-service-production get cronjobs,jobs
kubectl -n customer-service-production get deployment/postgres-wal-archive deployment/backup-metrics
