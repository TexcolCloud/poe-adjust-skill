"""Build the TeleAgent import ZIP; exclude development and machine data."""
from pathlib import Path
import re
import zipfile

root = Path(__file__).resolve().parent
source = root / "poe-adjust"
version = re.search(r'^VERSION = "([^"]+)"', (source / "scripts/poe.py").read_text(encoding="utf-8"), re.M).group(1)
destination = root / "dist" / f"poe-adjust-{version}.zip"
destination.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(source.rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
            archive.write(file, file.relative_to(root).as_posix())
print(f"{destination} ({destination.stat().st_size} bytes)")
