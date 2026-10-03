"""Build the TeleAgent import ZIP; exclude development and machine data."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent
source = root / "poe-adjust"
destination = root / "dist" / "poe-adjust-1.0.0.zip"
destination.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(source.rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
            archive.write(file, file.relative_to(root).as_posix())
print(f"{destination} ({destination.stat().st_size} bytes)")
