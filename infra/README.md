# Production infrastructure

The production platform consists of three one.com Cloud Server M instances and a
private fil.one S3-compatible bucket. OpenTofu manages three proxied Cloudflare DNS
records for round-robin origin selection. Ansible configures the hosts, WireGuard and
k3s. Argo CD manages Kubernetes workloads.

## Inputs that must remain outside Git

- Three public server addresses and verified SSH host keys.
- Per-node WireGuard private keys and the shared k3s token.
- The operator CIDR.
- Cloudflare API token with DNS edit access and the zone ID.
- fil.one access key, secret key, and a Restic repository password.
- Production age private key and application secret values.

Copy `ansible/inventory/production.example.yml` to the ignored
`ansible/inventory/production.yml` and encrypt its secret variables with
`ansible-vault`. Copy `infra/production/production.auto.tfvars.example` to an
untracked `.auto.tfvars` file and export the Cloudflare token:

```bash
export TF_VAR_cloudflare_api_token='...'
tofu -chdir=infra/production init -backend-config=backend.hcl
tofu -chdir=infra/production plan -out=production.tfplan
tofu -chdir=infra/production apply production.tfplan
```

The example backend deliberately uses an ignored local state path. Store an
encrypted copy of state with the recovery material; it contains origin addresses
and Cloudflare identifiers but no token.

## Bootstrap order

1. Verify the provisioned Ubuntu 26.04 hosts and add their SSH host fingerprints locally.
2. Generate unique WireGuard and age keys; store recovery copies independently.
3. Install the pinned Ansible version used by CI.
4. Run `ansible-playbook ansible/site.yml --ask-vault-pass` twice; the second run
   must report no unexpected changes.
5. Copy `/etc/rancher/k3s/k3s.yaml` from the first server and keep it in an ignored,
   mode-`0600` file. Keep the Kubernetes API private: either replace its server address
   with a WireGuard address on an operator device connected to that mesh, or use
   `https://127.0.0.1:6443` while an SSH tunnel is active:

   ```bash
   ssh -f -N -i /path/to/operator-key \
     -L 127.0.0.1:6443:10.50.0.11:6443 administrator@ORIGIN_1_PUBLIC_IP
   export KUBECONFIG="$PWD/local-docs/production-kubeconfig"
   ```

   Do not expose TCP 6443 publicly. Copy the full CA-bound server token from
   `/var/lib/rancher/k3s/server/token` into separately protected recovery material.
   SSH is allowlisted to `operator_cidr`. Before moving to a network with a new
   public address, update that CIDR and rerun the `common` role. If access is
   already blocked, use the one.com browser/serial console to add the new `/32`;
   do not temporarily open SSH to the internet.
6. Export the fil.one and Restic variables required by
   `scripts/init-fil-one-backups.sh`, initialize the encrypted cluster-state
   repository, then verify it with `scripts/check-production-backups.sh`.
7. Merge the deployment configuration so GitHub Actions creates `deploy/production`
   with real GHCR digests.
8. Install Helm 3, set `SOPS_AGE_KEY_FILE`, and run
   `scripts/bootstrap-production.sh`. Helm 4 is intentionally rejected because the
   pinned Argo CD chart has been validated with Helm 3. If both are installed, set
   `HELM_BIN` to the Helm 3 binary.
9. Commit the encrypted `SopsSecret`, then wait for Argo applications to become
   `Synced` and `Healthy`.
10. Confirm every origin directly with
    `curl --resolve css.ristoh.co.ke:443:ORIGIN_IP https://css.ristoh.co.ke/api/healthz`.
11. Apply the Cloudflare DNS plan only after all three direct checks succeed.

## Networking

k3s uses WireGuard addresses for the Kubernetes API, embedded etcd and Flannel.
UFW permits public TCP 80/443, WireGuard UDP between the three origins, and SSH only
from the operator CIDR. All cluster ports are accepted only on `wg0`. The bundled k3s
ServiceLB publishes Traefik on every node. Cloudflare proxies three same-name `A`
records and can select any origin, but this initial DNS-only configuration does not
perform active health checks or automatically remove a failed origin. Upgrade the
OpenTofu stack to Cloudflare Load Balancing before treating origin failover as automatic.

## Database and recovery

CloudNativePG keeps three local-storage PostgreSQL instances on distinct nodes. Local
volumes are not independently durable; quorum replication and the off-cluster copy
are both required. The fil.one bucket is never mounted as the live database filesystem.

The official Barman Cloud CNPG-I plugin continuously archives WAL files and creates
a daily physical base backup under `s3://ristoh-css-postgres/production/postgresql`.
The 180-day recovery window retains at least six months of daily recovery points.
CloudNativePG's five-minute default `archive_timeout` keeps the expected RPO below
15 minutes. A complete point-in-time restore drill must pass before production launch.

An encrypted Restic repository under `production/cluster-state` protects k3s
snapshots and credentials every six hours with 7 daily, 4 weekly and 6 monthly
snapshots. Preserve the Restic password and age key somewhere other than GitHub,
the cluster, and fil.one. Enable bucket versioning/object retention when fil.one
supports it, and rotate backup credentials independently of application credentials.
The fil.one credentials must permit object deletion within the production prefixes:
Barman retention and `restic forget --prune` cannot enforce retention otherwise.

## Operations

Run `scripts/validate-production.sh` after deployments and node maintenance. Test
failure one server at a time and confirm etcd quorum, PostgreSQL primary continuity,
Argo health and public ingress. Never test two simultaneous node failures: three-node
etcd and PostgreSQL designs tolerate one.
