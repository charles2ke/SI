import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from si.db import Chunk, Collection, Span
from si.rag import RAGService, chunk_text, parse_document, reciprocal_rank_fusion
from si.rag.network import UnsafeURLError, safe_fetch


class Embeddings:
    async def embed(self, texts):
        return [[1.0, *([0.0] * 63)] for _ in texts]


@pytest.fixture
def rag(database):
    with database() as session:
        session.add(Collection(id="documents", project_id="test-one", name="Documents"))
        session.commit()
        yield RAGService(session, Embeddings())


def test_chunk_overlap_and_bounds():
    assert chunk_text("abcdefghij", 5, 2) == ["abcde", "defgh", "ghij"]
    assert chunk_text("  ") == []
    assert chunk_text("abc", 3, 1) == ["abc"]
    for size, overlap in [(0, 0), (10, 10), (10, -1)]:
        with pytest.raises(ValueError):
            chunk_text("text", size, overlap)


def test_parse_html_text_and_limits():
    assert parse_document("a.txt", b"hello") == "hello"
    assert parse_document("a.html", b"<script>bad()</script><p>Hello</p>") == "Hello"
    with pytest.raises(ValueError, match="5 MB"):
        parse_document("a.txt", b"x" * 5_000_001)
    with pytest.raises(ValueError, match="Supported"):
        parse_document("a.exe", b"not text")


async def test_ingest_search_and_tenant_isolation(rag):
    assert await rag.ingest("test-one", "documents", "notes.md", b"Apple release notes") == 1
    hits = await rag.search("test-one", ["documents"], "Apple")
    assert hits[0]["text"] == "Apple release notes"
    assert hits[0]["score"] > 0
    kinds = {span.kind for span in rag.session.scalars(select(Span)).all()}
    assert {"embedding", "ingestion", "retrieval"} <= kinds
    with pytest.raises(ValueError, match="project"):
        await rag.search("test-two", ["documents"], "Apple")
    with pytest.raises(ValueError, match="project"):
        await rag.ingest("test-two", "documents", "a.txt", b"denied")
    assert await rag.search("test-two", [], "Apple") == []


async def test_ingest_atomic_embedding_validation(rag):
    class InvalidEmbeddings:
        async def embed(self, texts):
            return [[float("nan")] * 64 for _ in texts]

    rag.gateway = InvalidEmbeddings()
    with pytest.raises(ValueError, match="finite"):
        await rag.ingest("test-one", "documents", "a.txt", b"do not save")
    assert rag.session.scalars(select(Chunk)).all() == []


async def test_reranker(rag):
    await rag.ingest("test-one", "documents", "a.txt", b"one")
    await rag.ingest("test-one", "documents", "b.txt", b"two")
    rag.reranker = lambda query, rows: sorted(rows, key=lambda row: row["text"], reverse=True)
    assert (await rag.search("test-one", ["documents"], "one"))[0]["text"] == "two"


async def test_postgres_hybrid_uses_separate_vector_and_lexical_rankings():
    statements = []
    first = SimpleNamespace(id="semantic", text="meaning")
    second = SimpleNamespace(id="lexical", text="query")
    results = iter([["collection"], [first, second], [second]])

    class Results(list):
        def all(self):
            return self

    class Session:
        def add(self, row):
            pass

        def commit(self):
            pass

        def scalars(self, statement):
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return Results(next(results))

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    hits = await RAGService(Session(), Embeddings()).search("project", ["collection"], "query")
    assert hits[0]["id"] == "lexical"
    assert "<=>" in statements[1]
    assert "plainto_tsquery" in statements[2]
    assert "to_tsvector" in statements[2]


def test_rrf():
    assert reciprocal_rank_fusion([["a", "b"], ["b"]])[0][0] == "b"


async def test_network_blocks_private_and_default_deny(monkeypatch):
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(UnsafeURLError, match="Non-public"):
        await safe_fetch("http://internal.example")
    with pytest.raises(UnsafeURLError, match="allowlisted"):
        await safe_fetch("https://public.example", allowed_hosts=set())
    with pytest.raises(UnsafeURLError):
        await safe_fetch("file:///etc/passwd")
    with pytest.raises(UnsafeURLError):
        await safe_fetch("******public.example")


async def test_network_pins_dns_and_rechecks_redirect(monkeypatch):
    requests = []

    async def resolve(host, port, **kwargs):
        address = "127.0.0.1" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://private.example"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(UnsafeURLError, match="Non-public"):
        await safe_fetch("https://public.example", allowed_hosts={"public.example", "private.example"})
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "public.example"
    assert requests[0].extensions["sni_hostname"] == "public.example"


async def test_network_enforces_byte_limit(monkeypatch):
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    client_type = httpx.AsyncClient
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"large")),
            **kwargs,
        ),
    )
    with pytest.raises(ValueError, match="byte limit"):
        await safe_fetch("https://public.example", max_bytes=2)


def test_rag_http_api_ingest_search(client, redis):
    collection = client.post("/v1/collections", json={"name": "Docs"}).json()
    uploaded = client.post(
        f"/v1/collections/{collection['id']}/documents",
        files={"file": ("notes.txt", b"SI supports retrieval", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == {"chunks": 1}
    found = client.post(
        f"/v1/collections/{collection['id']}/search", json={"query": "retrieval"}
    )
    assert found.status_code == 200, found.text
    assert found.json()[0]["text"] == "SI supports retrieval"
    assert redis.usage[0] > 0
