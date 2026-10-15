"""Scan tracked files without printing credential values or matching source lines."""

import argparse
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret-file", action="append", default=[])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)  # noqa: S603, S607
    paths = [Path(p) for p in raw.decode().split("\0") if p]
    secrets = [Path(p).read_bytes().strip() for p in args.secret_file]
    patterns = [rb"AIza[0-9A-Za-z_-]{30,}", rb"gh[pousr]_[0-9A-Za-z]{20,}",
                rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"]
    errors = []
    for path in paths:
        if any(part in {"data", ".venv"} for part in path.parts) or path.name == ".env" or path.suffix in {".key", ".pem"}:
            errors.append(str(path) + ": forbidden runtime or credential file")
        content = (root / path).read_bytes()
        if any(secret and secret in content for secret in secrets):
            errors.append(str(path) + ": exact local credential detected")
        if path.name != "check_publication.py" and any(re.search(pattern, content) for pattern in patterns):
            errors.append(str(path) + ": credential pattern detected")
    if errors:
        raise SystemExit("Publication blocked:\n" + "\n".join(errors))
    print(f"Publication check passed: {len(paths)} tracked files; no credentials or runtime data.")


if __name__ == "__main__":
    main()
