"""Punto de entrada de Vercel: expone la app FastAPI (webapp.main) como función Python."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.main import create_app  # noqa: E402

app = create_app()
