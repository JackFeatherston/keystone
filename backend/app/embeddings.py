import httpx

from .config import settings


def embed(text: str, task: str = "search_document") -> list[float]:
    """Embed text with the local Ollama model (768 dims for nomic-embed-text). nomic-embed-text expects a
    task prefix: stored content is a search_document, queries use embed_query."""
    r = httpx.post(
        f"{settings.OLLAMA_URL}/api/embeddings",
        json={"model": settings.EMBEDDING_MODEL, "prompt": f"{task}: {text}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def embed_query(text: str) -> list[float]:
    return embed(text, "search_query")
