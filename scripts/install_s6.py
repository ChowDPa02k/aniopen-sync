"""Install pinned upstream s6-overlay archives into a Docker build stage."""

import argparse
import hashlib
import io
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "3.2.3.2"
CHECKSUMS = {
    "noarch": "5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12",
    "x86_64": "e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408",
    "aarch64": "b17f17a82e7a515c682a91edaf2ffdabb73f891981b6c1fd712115693a2f8b4c",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=["amd64", "arm64"], required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    for arch in ("noarch", {"amd64": "x86_64", "arm64": "aarch64"}[args.arch]):
        name = f"s6-overlay-{arch}.tar.xz"
        with urlopen(
            f"https://github.com/just-containers/s6-overlay/releases/download/v{VERSION}/{name}", timeout=60
        ) as response:
            content = response.read(16 * 1024 * 1024)
        if hashlib.sha256(content).hexdigest() != CHECKSUMS[arch]:
            raise ValueError(f"s6-overlay checksum mismatch: {name}")
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:xz") as archive:
            # Upstream archives contain absolute symlinks; extraction is confined to a disposable,
            # privileged Docker build stage and only occurs after verifying the pinned SHA-256.
            archive.extractall(args.destination, filter="fully_trusted")


if __name__ == "__main__":
    main()
