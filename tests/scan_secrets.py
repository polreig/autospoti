"""Escaneo de secretos: busca cadenas de 32 caracteres hexadecimales (formato de Client ID/Secret).

Uso:  python tests/scan_secrets.py   -> escribe scan_secrets_result.txt
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"\b[0-9a-fA-F]{32}\b")
SKIP_DIRS = {"data", "__pycache__", ".git", "venv", ".venv"}
EXTS = {".py", ".md", ".yml", ".yaml", ".ps1", ".txt", ".example", ".json", ".toml", ".clineignore", ".gitignore"}

hits = []
for path in ROOT.rglob("*"):
    if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
        continue
    if path.suffix not in EXTS and path.name not in {".env.example", ".clineignore", ".gitignore"}:
        continue
    if path.name == ".env":  # nunca leerlo ni mostrarlo
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for match in PATTERN.finditer(text):
        hits.append(f"{path.relative_to(ROOT)}: {match.group()[:6]}...")

(ROOT / "scan_secrets_result.txt").write_text(
    "\n".join(hits) if hits else "SIN COINCIDENCIAS", encoding="utf-8")
