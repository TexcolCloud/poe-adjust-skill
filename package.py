"""Build the TeleAgent import ZIP; exclude development and machine data."""
import argparse
from pathlib import Path
import re
import zipfile

root = Path(__file__).resolve().parent
source = root / "poe-adjust"
version = re.search(r'^VERSION = "([^"]+)"', (source / "scripts/poe.py").read_text(encoding="utf-8"), re.M).group(1)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--out', type=Path, help='Save a local verification package separately from an existing release.')
args = parser.parse_args()
destination = args.out.resolve() if args.out else root / "dist" / f"poe-adjust-{version}.zip"
if args.out and destination.exists(): parser.error('--out must be a new file; existing packages are preserved.')
destination.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(source.rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
            archive.write(file, file.relative_to(root).as_posix())
print(f"{destination} ({destination.stat().st_size} bytes)")
