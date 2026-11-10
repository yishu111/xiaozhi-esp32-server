#!/usr/bin/env python3
"""Raise Mem0's already-built SDK pool expiry without replacing library files.

pip install --force-reinstall on container start deletes site-packages edits.
This changes /app/main.py, which that command does not reinstall. The original
file must hash to the reviewed server/main.py. Secret lines are not rewritten.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import shutil
import sys
from pathlib import Path

EXPECTED_SHA256 = "e15b000724b024b335335a41b731696d9ae9f84a2efffca87451d38528c00d6c"
MARKER = "_xz_extend_keepalive_expiry"
BACKUP_SUFFIX = ".mem0-keepalive.bak"

INSERT = '''
def _xz_extend_keepalive_expiry(seconds=120.0):
    try:
        memory = get_memory_instance()
    except Exception:
        return
    for owner_name in ("embedding_model", "llm"):
        owner = getattr(memory, owner_name, None)
        client = getattr(owner, "client", None)
        http = getattr(client, "_client", None)
        if http is None:
            continue
        pools = []
        transport = getattr(http, "_transport", None)
        pool = getattr(transport, "_pool", None)
        if pool is not None:
            pools.append(pool)
        for mounted in getattr(http, "_mounts", {}).values():
            mounted_pool = getattr(mounted, "_pool", None)
            if mounted_pool is not None:
                pools.append(mounted_pool)
        for pool in pools:
            if hasattr(pool, "_keepalive_expiry"):
                pool._keepalive_expiry = seconds

'''


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_source(text: str) -> str:
    if MARKER in text:
        raise ValueError("already_present")
    tree = ast.parse(text)
    targets = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "initialize_state"
    ]
    if len(targets) != 1:
        raise ValueError(f"initialize_state_count {len(targets)}")
    lines = text.splitlines(keepends=True)
    insert_at = targets[0].end_lineno
    addition = INSERT + "_xz_extend_keepalive_expiry()\n"
    lines.insert(insert_at, addition if addition.endswith("\n") else addition + "\n")
    updated = "".join(lines)
    ast.parse(updated)
    return updated


def install(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    digest = sha256_text(text)
    if MARKER in text:
        return "already_installed"
    if digest != EXPECTED_SHA256:
        raise SystemExit(f"refusing_hash {digest}")
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists() and sha256_text(backup.read_text(encoding="utf-8")) != EXPECTED_SHA256:
        raise SystemExit("refusing_backup")
    updated = apply_source(text)
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
    path.write_text(updated, encoding="utf-8")
    return "installed"


def uninstall(path: Path) -> str:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        raise SystemExit("missing_backup")
    original = backup.read_text(encoding="utf-8")
    if sha256_text(original) != EXPECTED_SHA256:
        raise SystemExit("refusing_backup")
    shutil.copyfile(backup, path)
    if sha256_text(path.read_text(encoding="utf-8")) != EXPECTED_SHA256:
        raise SystemExit("restore_mismatch")
    return "restored"


def main() -> int:
    parser = argparse.ArgumentParser(description="Extend Mem0 SDK keepalive after init")
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args()
    result = install(args.path) if args.action == "install" else uninstall(args.path)
    sys.stdout.write(result + "\n")
    if result == "installed":
        sys.stdout.write(sha256_text(args.path.read_text(encoding="utf-8")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
