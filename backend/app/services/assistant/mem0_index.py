"""Transient Mem0 semantic index over the encrypted private-fact store."""

from __future__ import annotations

import os
import threading

_lock = threading.Lock()


def search(facts: list[tuple[str, str]], user_id: str, query: str, *,
           url: str, llm_model: str, embedding_model: str, dimensions: int) -> list[tuple[str, str]]:
    if not user_id or user_id == "*":
        raise ValueError("A specific user is required for Mem0 search")
    with _lock:
        # Mem0 reads this flag at import time. Do not initialize a telemetry-enabled SDK.
        os.environ["MEM0_TELEMETRY"] = "false"
        from mem0 import Memory
        from mem0.memory import telemetry
        from ollama import Client
        from qdrant_client import QdrantClient

        if telemetry.MEM0_TELEMETRY:
            raise RuntimeError("Mem0 was already initialized with telemetry enabled")
        client = QdrantClient(":memory:")
        memory = None
        try:
            memory = Memory.from_config({
                "vector_store": {
                    "provider": "qdrant",
                    "config": {"collection_name": "private_facts", "embedding_model_dims": dimensions,
                               "client": client},
                },
                "llm": {"provider": "ollama", "config": {
                    "model": llm_model, "ollama_base_url": url, "temperature": 0.1,
                }},
                "embedder": {"provider": "ollama", "config": {
                    "model": embedding_model, "embedding_dims": dimensions, "ollama_base_url": url,
                }},
                "history_db_path": ":memory:",
            })
            memory.embedding_model.client = Client(host=url, trust_env=False, follow_redirects=False, timeout=30)
            for row_id, fact in facts:
                memory.add(fact, user_id=user_id, metadata={"source_id": row_id}, infer=False)
            results = memory.search(query, filters={"user_id": user_id}, top_k=min(10, len(facts)),
                                    threshold=0.1, rerank=False)
            by_id = dict(facts)
            selected = []
            for item in results["results"]:
                metadata = item.get("metadata") or {}
                row_id = metadata.get("source_id")
                if row_id in by_id and item.get("memory") == by_id[row_id]:
                    selected.append((row_id, by_id[row_id]))
            return selected
        finally:
            if memory is not None:
                memory.db.connection.close()
            client.close()
