"""Online SQLite backups, including the atomic configuration and access token."""

import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import quote


def create_backup(data_dir: Path, target: Path):
    if not (data_dir / "history.sqlite3").exists():
        raise ValueError("数据目录尚无数据库，请先启动服务")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aniopen-backup-", dir=data_dir) as directory:
        snapshot = Path(directory) / "history.sqlite3"
        source = sqlite3.connect(
            "file:" + quote(str((data_dir / "history.sqlite3").resolve())) + "?mode=ro", uri=True
        )
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.chmod(snapshot, 0o600)
        fd, name = tempfile.mkstemp(prefix=".backup-", dir=target.parent)
        os.close(fd)
        try:
            with tarfile.open(name, "w:gz") as archive:
                archive.add(snapshot, arcname="history.sqlite3")
                for filename in ("config.yaml", "access-token", "mappings-cache.json"):
                    if (data_dir / filename).exists():
                        archive.add(data_dir / filename, arcname=filename)
            with open(name, "rb") as stream:
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return target
