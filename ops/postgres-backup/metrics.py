#!/usr/bin/env python3
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def command(*args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(args, env=env, text=True, timeout=30)


def repository_age(repository: str) -> float:
    env = os.environ | {"BORG_REPO": repository}
    archives = json.loads(command("borg", "list", "--json", env=env))["archives"]
    if not archives:
        return 1e99
    latest = max(
        time.mktime(time.strptime(item["start"][:19], "%Y-%m-%dT%H:%M:%S"))
        for item in archives
    )
    return max(0, time.time() - latest)


def metrics() -> str:
    ssh = os.environ["BORG_RSH"].split()
    target = f'{os.environ["STORAGE_BOX_USER"]}@{os.environ["STORAGE_BOX_HOST"]}'
    fields = command(*ssh, target, "df", "-Pk", ".").splitlines()[-1].split()
    total, available = int(fields[1]) * 1024, int(fields[3]) * 1024
    base = repository_age(os.environ["BASE_REPOSITORY"])
    wal = repository_age(os.environ["WAL_REPOSITORY"])
    return (
        f"storage_box_size_bytes {total}\n"
        f"storage_box_available_bytes {available}\n"
        f"postgres_base_backup_age_seconds {base}\n"
        f"postgres_wal_archive_age_seconds {wal}\n"
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = metrics().encode()
            self.send_response(200)
        except Exception:
            body = b"backup_metrics_scrape_error 1\n"
            self.send_response(500)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


HTTPServer(("0.0.0.0", 9101), Handler).serve_forever()
