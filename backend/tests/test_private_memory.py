import asyncio
import json
from types import SimpleNamespace

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models import LearnedResponse
from app.services.assistant import private_memory


def test_private_memory_is_encrypted_scoped_and_deletable(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'private.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(voice_encryption_key=Fernet.generate_key().decode())
    route = lambda user, text, verified=True: private_memory.route(factory, settings, user, text, verified=verified)

    assert "verify" in route("one", "remember that my home address is 12 Example Road", False)
    assert "saved" in route("one", "Remember that my girlfriend's name is Samira.")
    assert "saved" in route("one", "Keep in mind that my home address is 12 Example Road.")
    engine.dispose()
    factory = sessionmaker(create_engine(database_url))
    assert "Samira" in route("one", "What is my girlfriend's name?")
    assert "12 Example Road" in route("one", "Where is my home address?")
    assert "couldn't find" in route("two", "What is my girlfriend's name?")
    with factory() as db:
        rows = db.execute(select(LearnedResponse)).scalars().all()
        assert len(rows) == 2
        assert all("Samira" not in str(row.response) and "Example" not in str(row.response) for row in rows)
        assert all(row.tokens == [] and "girlfriend" not in row.prompt for row in rows)
    assert "deleted" in route("one", "Forget my home address")
    assert "couldn't find" in route("one", "Where is my home address?")
    assert "Samira" in route("one", "Who is my girlfriend?")
    assert "deleted" in route("one", "Forget everything")


def test_memory_rejects_secrets_and_does_not_intercept_other_features():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(voice_encryption_key=Fernet.generate_key().decode())
    assert "don't teach" in private_memory.route(factory, settings, "u", "remember that my password is secret", verified=True)
    for text in ("remember my face", "remember to call mom", "could you remember to call mom",
                 "what are my reminders", "show my notes"):
        assert private_memory.route(factory, settings, "u", text, verified=True) is None
    settings.voice_encryption_key = ""
    assert "unavailable" in private_memory.route(factory, settings, "u", "remember that my cat is named Pip", verified=True)


def test_natural_teaching_phrases_keep_original_fact():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(voice_encryption_key=Fernet.generate_key().decode())
    assert "saved" in private_memory.route(factory, settings, "u", "Could you remember that my cat is called Pip", verified=True)
    assert "Pip" in private_memory.route(factory, settings, "u", "What is my cat called?", verified=True)


def test_enabled_mem0_reports_missing_model():
    settings = SimpleNamespace(mem0_enabled=True, private_memory_model="")
    assert "no private memory model" in asyncio.run(
        private_memory.semantic_recall(None, settings, "u", "What is my pet called?", verified=True))


def test_semantic_recall_is_local_scoped_and_validates_selection(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'semantic.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(
        voice_encryption_key=Fernet.generate_key().decode(),
        private_memory_model="local-model",
        private_memory_ollama_url="http://127.0.0.1:11434",
    )
    private_memory.route(factory, settings, "one", "remember that my cat is named Pip", verified=True)
    private_memory.route(factory, settings, "two", "remember that my cat is named Spot", verified=True)
    payloads = []
    selection = {"id": 0}

    def handle(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"format": "gguf"}})
        body = json.loads(request.content)
        payloads.append(json.loads(body["messages"][1]["content"]))
        return httpx.Response(200, json={"message": {"content": json.dumps(selection)}})

    client = httpx.AsyncClient
    monkeypatch.setattr(private_memory.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))

    def recall(text="What is my pet called?", verified=True):
        return asyncio.run(private_memory.semantic_recall(factory, settings, "one", text, verified=verified))

    assert "Pip" in recall()
    assert payloads[0]["candidates"] == [{"id": 0, "fact": "my cat is named Pip"}]
    assert "verify" in recall(verified=False)
    assert recall("Forget my pet") is None
    assert len(payloads) == 1
    for selection in ({"id": True}, {"id": 9}, {"id": "0"}, {"id": 0, "extra": 1}):
        assert "invalid" in recall()
    selection = {"id": None}
    assert "couldn't find" in recall()
    settings.private_memory_ollama_url = "http://example.com:11434"
    assert "loopback-only" in recall()
    assert len(payloads) == 6


def test_semantic_recall_reports_model_failure_and_deleted_fact(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'deleted.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(
        voice_encryption_key=Fernet.generate_key().decode(),
        private_memory_model="local-model",
        private_memory_ollama_url="http://127.0.0.1:11434",
    )
    private_memory.route(factory, settings, "one", "remember that my cat is named Pip", verified=True)
    client = httpx.AsyncClient

    def deleted(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"format": "gguf"}})
        private_memory.route(factory, settings, "one", "forget everything", verified=True)
        return httpx.Response(200, json={"message": {"content": '{"id":0}'}})

    monkeypatch.setattr(private_memory.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(deleted), **kwargs))
    recall = lambda: asyncio.run(private_memory.semantic_recall(
        factory, settings, "one", "What is my pet called?", verified=True))
    assert "no longer available" in recall()
    private_memory.route(factory, settings, "one", "remember that my cat is named Pip", verified=True)
    monkeypatch.setattr(private_memory.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    assert "unavailable" in recall()


def test_semantic_recall_rejects_remote_model_before_sending_facts(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'remote.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = SimpleNamespace(voice_encryption_key=Fernet.generate_key().decode(),
                               private_memory_model="remote", private_memory_ollama_url="http://127.0.0.1:11434")
    private_memory.route(factory, settings, "one", "remember that my cat is named Pip", verified=True)
    paths = []

    def handle(request):
        paths.append(request.url.path)
        assert b"Pip" not in request.content
        return httpx.Response(200, json={"remote_host": "https://example.com", "details": {"format": "gguf"}})

    client = httpx.AsyncClient
    monkeypatch.setattr(private_memory.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    result = asyncio.run(private_memory.semantic_recall(
        factory, settings, "one", "What is my pet called?", verified=True))
    assert "not a remote model" in result
    assert paths == ["/api/show"]
