import os
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.services.assistant import mem0_index, private_memory


def test_real_mem0_sdk_indexes_and_searches_without_persistent_copy(monkeypatch, tmp_path):
    os.environ["MEM0_TELEMETRY"] = "false"
    pytest.importorskip("mem0")
    from mem0.embeddings.ollama import OllamaEmbedding

    monkeypatch.setattr(OllamaEmbedding, "_ensure_model_exists", lambda self: None)
    monkeypatch.setattr(OllamaEmbedding, "embed",
                        lambda self, text, memory_action=None: [1.0, 0.0] if "cat" in text else [0.0, 1.0])
    monkeypatch.chdir(tmp_path)
    facts = [("one", "my cat is called Pip"), ("two", "my dog is named Spot")]
    results = mem0_index.search(facts, "alice", "what is my cat called", url="http://127.0.0.1:11434",
                                llm_model="test", embedding_model="test", dimensions=2)
    assert results[0] == facts[0]
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError):
        mem0_index.search(facts, "*", "cat", url="http://127.0.0.1:11434",
                          llm_model="test", embedding_model="test", dimensions=2)


def test_mem0_backed_semantic_recall_uses_only_requesting_users_facts(monkeypatch, tmp_path):
    os.environ["MEM0_TELEMETRY"] = "false"
    pytest.importorskip("mem0")
    from mem0.embeddings.ollama import OllamaEmbedding

    monkeypatch.setattr(OllamaEmbedding, "_ensure_model_exists", lambda self: None)
    monkeypatch.setattr(OllamaEmbedding, "embed",
                        lambda self, text, memory_action=None: [1.0, 0.0])
    engine = create_engine(f"sqlite:///{tmp_path / 'facts.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(voice_encryption_key=Fernet.generate_key().decode(),
                               private_memory_model="chat", private_memory_ollama_url="http://127.0.0.1:11434",
                               mem0_enabled=True, mem0_embedding_model="embedding", mem0_embedding_dimensions=2)
    private_memory.route(factory, settings, "alice", "remember that my cat is named Pip", verified=True)
    private_memory.route(factory, settings, "bob", "remember that my cat is named Spot", verified=True)
    captured = []
    def handle(request):
        body = json.loads(request.content)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"format": "gguf"}})
        captured.append(json.loads(body["messages"][1]["content"]))
        return httpx.Response(200, json={"message": {"content": '{"id":0}'}})

    client = httpx.AsyncClient
    monkeypatch.setattr(private_memory.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    result = asyncio.run(private_memory.semantic_recall(
        factory, settings, "alice", "What is my pet called?", verified=True))
    assert "Pip" in result
    assert captured[0]["candidates"] == [{"id": 0, "fact": "my cat is named Pip"}]
    private_memory.route(factory, settings, "alice", "forget everything", verified=True)
    result = asyncio.run(private_memory.semantic_recall(
        factory, settings, "alice", "What is my pet called?", verified=True))
    assert "couldn't find" in result


def test_mem0_ollama_http_protocol_end_to_end(monkeypatch, tmp_path):
    os.environ["MEM0_TELEMETRY"] = "false"
    pytest.importorskip("mem0")
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond({"models": [{"name": "embed:latest", "model": "embed:latest"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(self.path)
            if self.path == "/api/show":
                self.respond({"details": {"format": "gguf"}})
            elif self.path == "/api/embed":
                self.respond({"model": "embed", "embeddings": [[1.0, 0.0]]})
            elif self.path == "/api/chat":
                self.respond({"message": {"content": '{"id":0}'}})
            else:
                self.send_error(404)

        def respond(self, obj):
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        engine = create_engine(f"sqlite:///{tmp_path / 'http.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        settings = SimpleNamespace(voice_encryption_key=Fernet.generate_key().decode(),
                                   private_memory_model="chat",
                                   private_memory_ollama_url=f"http://127.0.0.1:{server.server_port}",
                                   mem0_enabled=True, mem0_embedding_model="embed", mem0_embedding_dimensions=2)
        private_memory.route(factory, settings, "alice", "remember that my cat is named Pip", verified=True)
        result = asyncio.run(private_memory.semantic_recall(
            factory, settings, "alice", "What is my pet called?", verified=True))
        assert "Pip" in result
        assert seen.count("/api/show") == 2
        assert seen.count("/api/embed") >= 2
        assert "/api/chat" in seen
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.skipif(os.getenv("RUN_LOCAL_MEM0_TEST") != "1", reason="requires local Ollama models")
def test_local_ollama_and_mem0_with_encrypted_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    pytest.importorskip("mem0")
    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(
        voice_encryption_key=Fernet.generate_key().decode(),
        private_memory_model=os.getenv("LOCAL_MEM0_CHAT_MODEL", "smollm2:135m-instruct-q4_K_M"),
        private_memory_ollama_url="http://127.0.0.1:11434",
        mem0_enabled=True,
        mem0_embedding_model="all-minilm",
        mem0_embedding_dimensions=384,
    )
    try:
        assert "saved" in private_memory.route(
            factory, settings, "alice", "remember that my cat is named Pip", verified=True)
        assert "saved" in private_memory.route(
            factory, settings, "bob", "remember that my cat is named Spot", verified=True)
        result = asyncio.run(private_memory.semantic_recall(
            factory, settings, "alice", "What is my pet called?", verified=True))
        assert result == "You told me: my cat is named Pip"
        assert "deleted" in private_memory.route(factory, settings, "alice", "forget everything", verified=True)
        result = asyncio.run(private_memory.semantic_recall(
            factory, settings, "alice", "What is my pet called?", verified=True))
        assert "couldn't find" in result
    finally:
        engine.dispose()
