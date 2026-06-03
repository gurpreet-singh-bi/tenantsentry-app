"""
embedder.py
-----------
Converts text chunks into vector embeddings using Voyage AI.
voyage-large-2-instruct is optimised for retrieval tasks on legal/technical docs.
"""

import os
import time
import voyageai
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# Max retries on rate limit (429) errors
_MAX_RETRIES = 4
_RETRY_BASE_DELAY = 20  # seconds — Voyage free tier resets every 60s (3 RPM)

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

        for attempt in range(_MAX_RETRIES):
            try:
                result = client.embed(batch, model=model, input_type=input_type)
                all_embeddings.extend(result.embeddings)
                break
            except Exception as e:
                err = str(e)
                is_rate_limit = "429" in err or "rate limit" in err.lower() or "payment method" in err.lower()
                if is_rate_limit and attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BASE_DELAY * (attempt + 1)
                    logger.warning(f"Voyage rate limit hit — waiting {wait}s before retry {attempt + 1}/{_MAX_RETRIES - 1}")
                    time.sleep(wait)
                else:
                    raise

    logger.info(f"Embedded {len(texts)} texts → {len(all_embeddings)} vectors")
    return all_embeddings


def embed_query(query: str) -> list[float]:
    """Embed a single search query. Use input_type='query' for better retrieval."""
    return embed_texts([query], input_type="query")[0]
