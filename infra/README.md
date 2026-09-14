# Production infrastructure

The production platform consists of three one.com Cloud Server M instances and a
1-TB Hetzner Storage Box. OpenTofu manages Cloudflare DNS/load balancing. Ansible
configures the hosts, WireGuard and k3s. Argo CD manages Kubernetes workloads.

## Inputs that must remain outside Git

- Three public server addresses and verified SSH host keys.
- Per-node WireGuard private keys and the shared k3s token.
- The operator CIDR.
- Cloudflare API token, zone ID and notification address, plus a fine-grained GitHub
  token allowed to manage Actions variables for this repository.
- Storage Box hostname, username, SSH key and Borg passphrase.
- Production age private key and application secret values.

Copy `ansible/inventory/production.example.yml` to the ignored
`ansible/inventory/production.yml` and encrypt its secret variables with
`ansible-vault`. Copy `infra/production/production.auto.tfvars.example` to an
untracked `.auto.tfvars` file and export the Cloudflare token:

```bash
export TF_VAR_cloudflare_api_token='...'
export TF_VAR_github_token='...'
tofu -chdir=infra/production init -backend-config=backend.hcl
tofu -chdir=infra/production plan -out=production.tfplan
tofu -chdir=infra/production apply production.tfplan
```

The example backend deliberately uses an ignored local state path. Store an
encrypted copy of state with the recovery material; it contains origin addresses
and Cloudflare identifiers but no token.

## Bootstrap order

1. Install Ubuntu 24.04 LTS and add the actual SSH host fingerprints locally.
2. Generate unique WireGuard and age keys; store recovery copies independently.
3. Install the pinned Ansible version used by CI.
4. Run `ansible-playbook ansible/site.yml --ask-vault-pass` twice; the second run
   must report no unexpected changes.
5. Copy `/etc/rancher/k3s/k3s.yaml` from the first server, replace its server address
   with that server's WireGuard IP, and keep it only on an operator device connected
   to WireGuard.
6. Export the Storage Box variables required by `scripts/init-storage-box.sh`, run it
   to initialize `cluster-state`, `postgres-base`, and `postgres-wal`, then verify the
   repositories with `scripts/check-production-backups.sh`.
7. Merge the deployment configuration so GitHub Actions creates `deploy/production`
   with real GHCR digests.
8. Set `SOPS_AGE_KEY_FILE` and run `scripts/bootstrap-production.sh`.
9. Commit the encrypted `SopsSecret`, then wait for Argo applications to become
   `Synced` and `Healthy`.
10. Confirm every origin directly with
    `curl --resolve css.ristoh.co.ke:443:ORIGIN_IP https://css.ristoh.co.ke/api/healthz`.
11. Apply the Cloudflare plan only after all three direct checks succeed.

## Networking

k3s uses WireGuard addresses for the Kubernetes API, embedded etcd and Flannel.
UFW permits public TCP 80/443, WireGuard UDP between the three origins, and SSH only
from the operator CIDR. All cluster ports are accepted only on `wg0`. The bundled k3s
ServiceLB publishes Traefik on every node so Cloudflare can health-check each origin.

## Database and recovery

CloudNativePG keeps three local-storage PostgreSQL instances on distinct nodes. Local
volumes are not independently durable; quorum replication and the off-cluster copy
are both required. The Storage Box is never mounted as the live database filesystem.

CloudNativePG's native continuous-backup integration requires S3-compatible object
storage and cannot write directly to a Storage Box. Meeting the selected 15-minute
RPO therefore requires a separately validated WAL receiver/archive job. Do not launch
production until that job, daily physical base backups, alerting, and a complete
two-hour restore drill have passed. If this operational path proves unreliable, add
S3-compatible object storage and use the official Barman Cloud plugin.

Host Borg jobs protect k3s snapshots and credentials every six hours. Retention is
7 daily, 4 weekly and 6 monthly archives. Configure Storage Box snapshots and alert
at 80% capacity. Preserve Borg and age keys somewhere other than GitHub, the cluster,
and the Storage Box.

## Operations

Run `scripts/validate-production.sh` after deployments and node maintenance. Test
failure one server at a time and confirm etcd quorum, PostgreSQL primary continuity,
Argo health and public ingress. Never test two simultaneous node failures: three-node
etcd and PostgreSQL designs tolerate one.
