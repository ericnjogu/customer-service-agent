FROM postgres:17.11-bookworm

RUN apt-get update \
    && apt-get install --yes --no-install-recommends borgbackup openssh-client ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=postgres:postgres metrics.py /usr/local/bin/backup-metrics
RUN chmod 0555 /usr/local/bin/backup-metrics

USER postgres
