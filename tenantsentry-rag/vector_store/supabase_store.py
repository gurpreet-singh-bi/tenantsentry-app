"""
supabase_store.py
-----------------
Stores and retrieves chunk embeddings from Supabase (pgvector).
Table: lease_chunks

Run this SQL once in Supabase SQL editor to set up:

    CREATE EXTENSION IF NOT EXISTS vector;

    CREATE TABLE lease_chunks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        content TEXT NOT NULL,
        embedding VECTOR(1024),
        metadata JSONB,
        document_id TEXT,
        chunk_type TEXT CHECK (chunk_type IN ('lease', 'legislation', 'rule')),
        jurisdiction TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX ON lease_chunks USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);

    CREATE INDEX ON lease_chunks (chunk_type);
    CREATE INDEX ON lease_chunks (jurisdiction);
"""

import os
from supabase import create_client, Client
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_client = None


def get_client() -> Client:
    global _client
    if _client is None:
        import httpx
        from supabase.lib.client_options import ClientOptions
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
            options=ClientOptions(
                postgrest_client_timeout=httpx.Timeout(connect=5.0, read=30.0, write=60.0, pool=5.0)
            ),
        )
    return _client


def document_exists(document_hash: str) -> bool:
    """
    G2: Check whether a document has already been embedded in the vector store.

    Uses the SHA-256 hash stored in document_id.  A single matching row is
    enough — if the first chunk of a document is present, the full document
    was embedded on a prior upload.

    Returns True  → skip embedding, chunks already indexed.
    Returns False → proceed with embedding.
    """
    try:
        client = get_client()
        result = (
            client.table("lease_chunks")
            .select("id")
            .eq("document_id", document_hash)
            .limit(1)
            .execute()
        )
        exists = len(result.data) > 0
        if exists:
            logger.info(f"G2: document_id={document_hash[:12]}… already in vector store — dedup hit")
        return exists
    except Exception as e:
        # If the check itself fails, default to re-embedding (safe degradation)
        logger.warning(f"G2: document_exists check failed ({e}) — will re-embed to be safe")
        return False


def upsert_chunks(chunks: list[dict]) -> None:
    """
    Insert chunks into the vector store.

    Each chunk dict must have:
        content, embedding, metadata, document_id, chunk_type, jurisdiction

    Call document_exists(document_hash) before this to skip duplicates (G2).
    document_id should be set to the SHA-256 hash of the source PDF so that
    document_exists() can detect re-uploads of the same file.
    """
    client = get_client()

    rows = [
        {
            "content":     c["content"],
            "embedding":   c["embedding"],
            "metadata":    c.get("metadata", {}),
            "document_id": c.get("document_id"),
            "chunk_type":  c.get("chunk_type", "lease"),
            "jurisdiction": c.get("jurisdiction"),
        }
        for c in chunks
    ]

    try:
        result = client.table("lease_chunks").insert(rows).execute()
        logger.info(f"Inserted {len(rows)} chunks into Supabase (document_id={rows[0].get('document_id','?')[:12]}…)")
        return result
    except Exception as e:
        # Non-fatal for the audit — vector store embedding fails gracefully.
        # The audit pipeline continues without stored embeddings.
        logger.error(f"upsert_chunks failed ({len(rows)} rows): {e}")
        return None


def similarity_search(
    query_embedding: list[float],
    top_k: int = 8,
    chunk_type: str = None,
    jurisdiction: str = None,
) -> list[dict]:
    """
    Find the top_k most relevant chunks via cosine similarity.

    Args:
        query_embedding: 1024-dim vector from embed_query()
        top_k: Number of results to return
        chunk_type: Filter by 'lease', 'legislation', or 'rule'
        jurisdiction: Filter by state code e.g. 'NSW', 'VIC'

    Returns:
        List of {content, metadata, similarity_score} dicts
    """
    client = get_client()

    # Use Supabase RPC for vector similarity search
    # Requires this function in Supabase:
    #
    #   CREATE OR REPLACE FUNCTION match_chunks(
    #       query_embedding VECTOR(1024),
    #       match_count INT,
    #       filter_chunk_type TEXT DEFAULT NULL,
    #       filter_jurisdiction TEXT DEFAULT NULL
    #   )
    #   RETURNS TABLE (id UUID, content TEXT, metadata JSONB, similarity FLOAT)
    #   LANGUAGE SQL STABLE AS $$
    #       SELECT id, content, metadata,
    #              1 - (embedding <=> query_embedding) AS similarity
    #       FROM lease_chunks
    #       WHERE (filter_chunk_type IS NULL OR chunk_type = filter_chunk_type)
    #         AND (filter_jurisdiction IS NULL OR jurisdiction = filter_jurisdiction)
    #       ORDER BY embedding <=> query_embedding
    #       LIMIT match_count;
    #   $$;

    result = client.rpc("match_chunks", {
        "query_embedding": query_embedding,
        "match_count": top_k,
        "filter_chunk_type": chunk_type,
        "filter_jurisdiction": jurisdiction,
    }).execute()

    logger.debug(f"Retrieved {len(result.data)} chunks (type={chunk_type}, jurisdiction={jurisdiction})")
    return result.data
