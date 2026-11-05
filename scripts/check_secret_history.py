#!/usr/bin/env python3
"""Check whether any secret in the local .env also appears in git history.

Run from the repo root. Prints key names and match counts, never values. This
repo is public, so keep the output out of issues, PRs and logs: it says which
leaked values are still live.

Exit status: 0 if no match, 1 if any current secret is found in history.
"""
import subprocess
import sys

SECRET_NAME_PARTS = ('KEY', 'SECRET', 'PASSWORD', 'TOKEN', 'INVITE')
MAX_BLOB_BYTES = 8_000_000


def read_secrets(path='.env'):
    secrets = {}
    with open(path) as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            name, value = line.split('=', 1)
            name, value = name.strip(), value.strip().strip('"\'')
            if value and any(part in name for part in SECRET_NAME_PARTS):
                secrets[name] = value
    return secrets


def history_blobs():
    """Yield (sha, path) for every blob reachable from any ref, once per sha."""
    listing = subprocess.run(['git', 'rev-list', '--all', '--objects'],
                             capture_output=True, text=True, check=True).stdout
    seen = {}
    for line in listing.splitlines():
        parts = line.split(' ', 1)
        if len(parts) == 2:
            seen.setdefault(parts[0], parts[1])
    return seen


def main():
    secrets = read_secrets()
    matches = {name: set() for name in secrets}
    blobs = history_blobs()
    for sha, path in blobs.items():
        result = subprocess.run(['git', 'cat-file', 'blob', sha], capture_output=True)
        if result.returncode or len(result.stdout) > MAX_BLOB_BYTES:
            continue
        for name, value in secrets.items():
            if value.encode() in result.stdout:
                matches[name].add(path)

    print(f'Checked {len(secrets)} secrets against {len(blobs)} paths in history.')
    for name in sorted(matches):
        paths = ', '.join(sorted(matches[name])) or '-'
        print(f"{name}: {'IN HISTORY' if matches[name] else 'ok'} ({paths})")
    return 1 if any(matches.values()) else 0


if __name__ == '__main__':
    sys.exit(main())
