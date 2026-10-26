import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    project_root: Path
    gemini_api_key: str
    gemini_model: str
    embed_model: str
    chroma_dir: Path
    similarity_threshold: float


def _resolve_from_root(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def _load_settings() -> Settings:
    return Settings(
        project_root=PROJECT_ROOT,
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        embed_model=os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2"),
        chroma_dir=_resolve_from_root(os.getenv("CHROMA_DIR", "./chroma_db")),
        similarity_threshold=float(os.getenv("SIMILARITY_THRESHOLD", "0.30")),
    )


settings = _load_settings()
