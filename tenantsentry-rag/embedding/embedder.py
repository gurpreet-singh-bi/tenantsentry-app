"""
embedder.py
-----------
Converts text chunks into vector embeddings using Voyage AI.
voyage-large-2-instruct is optimised for retrieval tasks on legal/technical docs.
"""

import os
import voyageai
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_client = None


def get_client() -> voyageai.Client:
    global _client
    if _client is None:
        _client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
    return _client


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """
    Embed a list of texts. Returns list of 1024-dim vectors.

    Args:
        texts: List of text strings to embed
        input_type: "document" for KB/lease chunks, "query" for search queries

    Returns:
        List of embedding vectors (each is a list of 1024 floats)
    """
    if not texts:
        return []

    client = get_client()
    model = os.environ.get("EMBEDDING_MODEL", "voyage-large-2-instruct")

    # Voyage API has a batch limit — process in batches of 128
    all_embeddings = []
    batch_size = 128

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        logger.debug(f"Embedding batch {i//batch_size + 1} ({len(batch)} texts)")
        result = client.embed(batch, model=model, input_type=input_type)
        all_embeddings.extend(result.embeddings)

    logger.info(f"Embedded {len(texts)} texts → {len(all_embeddings)} vectors")
    return all_embeddings


def embed_query(query: str) -> list[float]:
    """Embed a single search query. Use input_type='query' for better retrieval."""
    return embed_texts([query], input_type="query")[0]
