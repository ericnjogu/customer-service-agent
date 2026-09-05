#!/usr/bin/env python3
"""Update only generated staging endpoints, ARNs, and image digests."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def replace_once(text: str, pattern: str, replacement: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"expected exactly one match for {pattern!r}, found {count}")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=Path("helm/customer-service/values-staging.yaml"))
    parser.add_argument("--app-digest")
    parser.add_argument("--web-digest")
    parser.add_argument("--database-secret-arn")
    parser.add_argument("--database-host")
    parser.add_argument("--cache-host")
    args = parser.parse_args()

    text = args.file.read_text()
    if args.app_digest:
        text = replace_once(
            text,
            r"^(image:\n(?:  .*\n)*?  digest:) .*$",
            rf"\1 {args.app_digest}",
        )
    if args.web_digest:
        text = replace_once(
            text,
            r"^(web:\n(?:  .*\n)*?  image:\n(?:    .*\n)*?    digest:) .*$",
            rf"\1 {args.web_digest}",
        )
    if args.database_secret_arn:
        text = replace_once(text, r"^(  secretArn:) .*$", rf"\1 {args.database_secret_arn}")
    if args.database_host:
        text = replace_once(text, r"^(  host:) .*$", rf"\1 {args.database_host}")
    if args.cache_host:
        text = replace_once(text, r"^(  url:) .*$", rf"\1 rediss://{args.cache_host}:6379/0")
    args.file.write_text(text)


if __name__ == "__main__":
    main()
