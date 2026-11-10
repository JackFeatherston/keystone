import httpx

from .config import settings


def embed(text: str) -> list[float]:
    """Embed text with the local Ollama model (768 dims for nomic-embed-text)."""
    r = httpx.post(
        f"{settings.OLLAMA_URL}/api/embeddings",
        json={"model": settings.EMBEDDING_MODEL, "prompt": text},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["embedding"]
