#!/usr/bin/env python3
"""Configure the freshly initialized vault without exposing bootstrap credentials."""

import json
import os
import ssl
import subprocess
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    recovery = root / "local-docs/openbao-recovery"
    material = json.loads((recovery / "initialization.json").read_text())
    context = ssl.create_default_context(cafile=str(recovery / "ca.crt"))
    port = int(os.getenv("OPENBAO_BOOTSTRAP_PORT", "18201"))
    with httpx.Client(
        base_url=f"https://localhost:{port}",
        verify=context,
        timeout=30,
        headers={"X-Vault-Token": material["root_token"]},
    ) as client:

        def request(method, path, **kwargs):
            response = client.request(method, "/v1/" + path, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else {}

        mounts = request("GET", "sys/mounts")
        if "tenant-credentials/" not in mounts.get("data", mounts):
            request(
                "POST",
                "sys/mounts/tenant-credentials",
                json={"type": "kv", "options": {"version": "2"}},
            )
        auth = request("GET", "sys/auth")
        if "kubernetes/" not in auth.get("data", auth):
            request("POST", "sys/auth/kubernetes", json={"type": "kubernetes"})
        request(
            "POST",
            "auth/kubernetes/config",
            json={"kubernetes_host": "https://kubernetes.default.svc"},
        )
        for policy, file in (
            ("customer-service", "application"),
            ("snapshot", "snapshot"),
            ("operator", "operator"),
        ):
            request(
                "PUT",
                f"sys/policies/acl/{policy}",
                json={"policy": (root / f"infra/openbao/{file}-policy.hcl").read_text()},
            )
        roles = [
            ("customer-service", "one-css-customer-service-app", "customer-service-production"),
            ("snapshot", "openbao-backup", "openbao"),
            ("operator", "openbao-operator", "openbao"),
        ]
        for role, account, namespace in roles:
            request(
                "POST",
                f"auth/kubernetes/role/{role}",
                json={
                    "bound_service_account_names": [account],
                    "bound_service_account_namespaces": [namespace],
                    "audience": "openbao",
                    "token_policies": [role],
                    "token_ttl": "10m",
                    "token_max_ttl": "15m",
                },
            )
        audit = request("GET", "sys/audit")
        if "to-stdout/" not in audit.get("data", audit):
            raise RuntimeError(
                "Declarative audit device is not yet active; restart replicas safely"
            )
        jwt = subprocess.run(
            [
                "kubectl",
                "-n",
                "openbao",
                "create",
                "token",
                "openbao-operator",
                "--audience=openbao",
                "--duration=10m",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        auth = request("POST", "auth/kubernetes/login", json={"role": "operator", "jwt": jwt})
        token = auth["auth"]["client_token"]
        response = client.get(
            "/v1/sys/storage/raft/configuration", headers={"X-Vault-Token": token}
        )
        response.raise_for_status()
        servers = response.json()["data"]["config"]["servers"]
        if len(servers) != 3 or not all(server["voter"] for server in servers):
            raise RuntimeError("Expected three voting Raft members")
        denied = client.get("/v1/sys/policies/acl/root", headers={"X-Vault-Token": token})
        if denied.status_code != 403:
            raise RuntimeError("Operator policy must not permit root-policy reads")
        client.post(
            "/v1/auth/token/revoke-self", headers={"X-Vault-Token": token}
        ).raise_for_status()
    print("Configured KV, Kubernetes roles, restricted policies and redacted audit logging.")
    print("Verified operator login, three voting peers, and denied privileged policy access.")
    print("Bootstrap root remains temporary pending final validation and revocation.")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as error:
        raise SystemExit(
            f"Vault configuration stopped: {error.request.method} {error.request.url.path} "
            f"returned HTTP {error.response.status_code}; response body suppressed."
        ) from None
    except Exception as error:
        raise SystemExit(
            f"Vault configuration stopped ({type(error).__name__}); no values displayed."
        ) from None
