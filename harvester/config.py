"""Configuratie uit .env. Eén plek waar paden en credentials vandaan komen."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")


def _clean(value: str | None) -> str | None:
    """Lege strings uit .env behandelen als 'niet ingevuld'."""
    if value is None:
        return None
    value = value.strip().strip('"').strip("'")
    return value or None


@dataclass(frozen=True)
class Config:
    ncbi_api_key: str | None
    ncbi_email: str | None
    ncbi_tool: str
    unpaywall_email: str | None
    data_dir: Path
    pdf_dir: Path | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "metadata" / "nerve_lit.db"

    @property
    def lancedb_path(self) -> Path:
        return self.data_dir / "lancedb"

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def require_pdf_dir(self) -> Path:
        """Voor fase 2. Faalt luid als de Drive-map niet bestaat — beter dan
        stilletjes 40k PDF's naar een lokale map schrijven die niet synct."""
        if self.pdf_dir is None:
            raise RuntimeError("PDF_DIR staat niet in .env — zie .env.example")
        if not self.pdf_dir.parent.exists():
            raise RuntimeError(
                f"PDF_DIR-ouder bestaat niet: {self.pdf_dir.parent} — "
                "is Google Drive for Desktop gemount?"
            )
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        return self.pdf_dir


def load_config() -> Config:
    data_dir = Path(_clean(os.getenv("DATA_DIR")) or "data")
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir
    pdf_dir_raw = _clean(os.getenv("PDF_DIR"))
    return Config(
        ncbi_api_key=_clean(os.getenv("NCBI_API_KEY")),
        ncbi_email=_clean(os.getenv("NCBI_EMAIL")),
        ncbi_tool=_clean(os.getenv("NCBI_TOOL")) or "nerve-lit",
        unpaywall_email=_clean(os.getenv("UNPAYWALL_EMAIL")),
        data_dir=data_dir,
        pdf_dir=Path(pdf_dir_raw) if pdf_dir_raw else None,
    )
