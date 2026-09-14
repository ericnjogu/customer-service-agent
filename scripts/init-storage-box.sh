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
  if borg info >/dev/null 2>&1; then
    echo "Repository already initialized: ${repository}"
  else
    borg init --encryption=repokey-blake2
    echo "Initialized encrypted repository: ${repository}"
  fi
done
