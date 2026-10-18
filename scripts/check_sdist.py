"""Check the supported source distribution really contains a buildable, testable checkout."""

import argparse
import subprocess
import tarfile
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args()
    archive = max(Path("dist").glob("aniopen_sync-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    required = {
        ".dockerignore",
        "tests/conftest.py",
        "web/package-lock.json",
        "web/src/main.tsx",
        "web/index.html",
        "web/vite.config.ts",
        "web/tsconfig.json",
        "Dockerfile",
        "scripts/install_s6.py",
        "THIRD_PARTY_NOTICES",
        "compose.yaml",
        "uv.lock",
    }
    with tempfile.TemporaryDirectory(prefix="aniopen-source-") as directory:
        with tarfile.open(archive) as source:
            names = {name.split("/", 1)[1] for name in source.getnames() if "/" in name}
            assert required <= names, f"Missing source assets: {required - names}"
            assert not any("/node_modules/" in name or name.startswith("data/") for name in names)
            source.extractall(directory, filter="data")
        root = next(Path(directory).iterdir())
        if args.run_tests:
            subprocess.run(["uv", "sync", "--locked", "--extra", "dev"], cwd=root, check=True)
            subprocess.run(["uv", "run", "pytest", "-q"], cwd=root, check=True)
    print("Source distribution assets verified")


if __name__ == "__main__":
    main()
