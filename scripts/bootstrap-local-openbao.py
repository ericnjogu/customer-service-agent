#!/usr/bin/env python3
"""Persistent, isolated Rancher Desktop vault. Never uses production recovery files."""

import argparse
import base64
import json
import os
import re
import socket
import ssl
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
VAULT_NAMESPACE = "openbao-local"
VAULT_RELEASE = "openbao-local"
META_KEYS = (
    "AGENT_META_APP_ID",
    "AGENT_META_SIGNUP_CONFIGURATION_ID",
    "AGENT_META_APP_SECRET",
    "AGENT_META_WEBHOOK_VERIFICATION_TOKEN",
)


def run(args, **kwargs):
    try:
        return subprocess.run(args, capture_output=True, check=True, timeout=180, **kwargs).stdout
    except subprocess.CalledProcessError as error:
        # Commands carry only resource names/paths; secret payloads use stdin.
        raise ValueError(
            f"Local command failed ({error.returncode}): {' '.join(args[:6])}; "
            "captured output suppressed"
        ) from None


def validate(args):
    if args.context != "rancher-desktop":
        raise ValueError("Local vault bootstrap requires KUBE_CONTEXT=rancher-desktop")
    for value in (args.namespace, args.service_account, args.role, args.meta_secret):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value):
            raise ValueError("Namespace, service account, role and Secret must be DNS labels")
    if args.namespace in {"openbao", "customer-service-production", "openbao-local"}:
        raise ValueError("Use a dedicated local application namespace")


def private_write(path, content):
    # Exclusive creation prevents silently replacing an existing recovery key.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def configure(client, namespace, account, role):
    def request(method, path, **kwargs):
        result = client.request(method, "/v1/" + path, **kwargs)
        result.raise_for_status()
        return result.json() if result.content else {}

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
        "POST", "auth/kubernetes/config", json={"kubernetes_host": "https://kubernetes.default.svc"}
    )
    request(
        "PUT",
        "sys/policies/acl/customer-service",
        json={"policy": (ROOT / "infra/openbao/application-policy.hcl").read_text()},
    )
    request(
        "POST",
        f"auth/kubernetes/role/{role}",
        json={
            "bound_service_account_names": [account],
            "bound_service_account_namespaces": [namespace],
            "audience": "openbao",
            "token_policies": ["customer-service"],
            "token_ttl": "10m",
            "token_max_ttl": "15m",
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--meta-secret", default="app-configs")
    args = parser.parse_args()
    validate(args)
    os.umask(0o077)
    kubectl = ["kubectl", "--context", args.context]
    # Check only key names/non-emptiness, never emit Meta values or copy from prod.
    present = (
        run(
            kubectl
            + [
                "get",
                "secret",
                args.meta_secret,
                "-n",
                args.namespace,
                "-o",
                'go-template={{range $k, $v := .data}}{{if $v}}{{$k}}{{"\\n"}}{{end}}{{end}}',
            ]
        )
        .decode()
        .splitlines()
    )
    missing = sorted(set(META_KEYS) - set(present))
    if missing:
        raise ValueError("Local app-configs is missing keys: " + ", ".join(missing))

    recovery = ROOT / "local-docs/openbao-local/rancher-desktop"
    recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
    recovery.chmod(0o700)
    seal = recovery / "seal-key"
    # Refuse fresh keys if old PVCs/Secrets exist; never destroy or reset a vault.
    existing_json = run(
        kubectl + ["get", "pvc,secret", "-n", VAULT_NAMESPACE, "--ignore-not-found", "-o", "json"]
    )
    existing = json.loads(existing_json) if existing_json.strip() else {"items": []}
    if not seal.exists():
        if existing.get("items"):
            raise ValueError(
                "Local vault resources exist but seal key is missing; restore local recovery files"
            )
        private_write(seal, os.urandom(32))
    if len(seal.read_bytes()) != 32:
        raise ValueError("Local seal key must contain exactly 32 bytes")
    for item in existing.get("items", []):
        if item["kind"] == "Secret" and item["metadata"]["name"] == "openbao-seal":
            if base64.b64decode(item["data"]["key"]) != seal.read_bytes():
                raise ValueError(
                    "Local seal key differs from the installed key; refusing replacement"
                )

    certificate, key = recovery / "tls.crt", recovery / "tls.key"
    if certificate.exists() != key.exists():
        raise ValueError("Incomplete local TLS material; restore both certificate and key")
    if not certificate.exists():
        if existing.get("items"):
            raise ValueError("Local vault exists but TLS recovery files are missing")
        config = recovery / "tls.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=local-openbao\n[ext]\nbasicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
            "subjectAltName=DNS:localhost,DNS:openbao-local-active.openbao-local.svc,"
            "DNS:openbao-local.openbao-local.svc,IP:127.0.0.1\n"
        )
        run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "365",
                "-config",
                str(config),
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ]
        )
    run(["openssl", "x509", "-checkend", "0", "-noout", "-in", str(certificate)])

    def apply(document):
        run(
            kubectl
            + ["apply", "--server-side", "--field-manager=local-openbao-bootstrap", "-f", "-"],
            input=json.dumps(document).encode(),
        )

    apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": VAULT_NAMESPACE}})
    for name, data in (
        ("openbao-seal", {"key": seal.read_bytes()}),
        (
            "openbao-tls",
            {
                "tls.crt": certificate.read_bytes(),
                "tls.key": key.read_bytes(),
                "ca.crt": certificate.read_bytes(),
            },
        ),
    ):
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": VAULT_NAMESPACE},
                "data": {k: base64.b64encode(v).decode() for k, v in data.items()},
            }
        )
    chart = ROOT / "helm/openbao-platform"
    run(
        [
            "helm",
            "repo",
            "add",
            "customer-service-openbao",
            "https://openbao.github.io/openbao-helm",
        ]
    )
    run(["helm", "dependency", "build", str(chart)])
    run(
        [
            "helm",
            "--kube-context",
            args.context,
            "upgrade",
            "--install",
            VAULT_RELEASE,
            str(chart),
            "-n",
            VAULT_NAMESPACE,
            "-f",
            str(chart / "values-local.yaml"),
        ]
    )
    run(
        kubectl
        + ["wait", "-n", VAULT_NAMESPACE, "pod/openbao-local-0", "--for=create", "--timeout=120s"]
    )
    run(
        kubectl
        + [
            "wait",
            "-n",
            VAULT_NAMESPACE,
            "pod/openbao-local-0",
            "--for=jsonpath={.status.phase}=Running",
            "--timeout=120s",
        ]
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    forward = subprocess.Popen(
        kubectl
        + [
            "port-forward",
            "-n",
            VAULT_NAMESPACE,
            "pod/openbao-local-0",
            f"{port}:8200",
            "--address=127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(
            base_url=f"https://localhost:{port}",
            timeout=30,
            verify=ssl.create_default_context(cafile=str(certificate)),
        ) as client:
            for _ in range(60):
                if forward.poll() is not None:
                    raise ValueError("Local vault port-forward failed")
                try:
                    response = client.get("/v1/sys/init")
                    response.raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(1)
            else:
                raise ValueError("Local vault TLS endpoint did not become available")
            material = recovery / "initialization.json"
            if not response.json()["initialized"]:
                if material.exists():
                    raise ValueError(
                        "Recovery output exists but vault is uninitialized; refusing reset"
                    )
                # Open recovery destination before the irreversible initialization.
                fd = os.open(material, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    response = client.put(
                        "/v1/sys/init", json={"recovery_shares": 1, "recovery_threshold": 1}
                    )
                    response.raise_for_status()
                    output.write(response.content)
                    output.flush()
                    os.fsync(output.fileno())
            if not material.exists():
                raise ValueError("Initialized local vault has no recovery file; restore it")
            client.headers["X-Vault-Token"] = json.loads(material.read_text())["root_token"]
            for _ in range(30):
                status = client.get("/v1/sys/health").json()
                if not status.get("sealed", True):
                    break
                time.sleep(1)
            else:
                raise ValueError("Local vault did not auto-unseal")
            configure(client, args.namespace, args.service_account, args.role)
        apply(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "openbao-local-ca", "namespace": args.namespace},
                "data": {"ca.crt": certificate.read_text()},
            }
        )
        run(
            kubectl
            + [
                "rollout",
                "status",
                "statefulset/openbao-local",
                "-n",
                VAULT_NAMESPACE,
                "--timeout=120s",
            ]
        )
    finally:
        forward.terminate()
        forward.wait(timeout=10)
    print(
        "Local OpenBao ready: one persistent replica, TLS and namespace-bound app authentication."
    )
    print("Local-only recovery material: local-docs/openbao-local/rancher-desktop (keep private).")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        raise SystemExit(
            f"Local vault bootstrap failed ({type(error).__name__}); secret values suppressed."
        ) from None
