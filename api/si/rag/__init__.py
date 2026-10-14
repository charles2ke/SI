"""Project-scoped ingestion and hybrid retrieval."""

from __future__ import annotations

import io
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import cast, select

from .network import safe_fetch

MAX_DOCUMENT_BYTES = 5_000_000
MAX_DOCUMENT_CHARACTERS = 2_000_000


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    if not 1 <= chunk_size <= 20_000 or not 0 <= overlap < chunk_size:
        raise ValueError("Require 1 <= chunk_size <= 20000 and 0 <= overlap < chunk_size")
    text = text.strip()
    if not text:
        return []
    step = chunk_size - overlap
    chunks = []
    for start in range(0, len(text), step):
        chunks.append(text[start : start + chunk_size])
        if start + chunk_size >= len(text):
            break
    return chunks


def parse_document(filename: str, data: bytes) -> str:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds 5 MB")
    extension = Path(filename).suffix.lower()
    if extension == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are unsupported")
        if len(reader.pages) > 500:
            raise ValueError("PDF exceeds 500 pages")
        parts = []
        characters = 0
        for page in reader.pages:
            part = page.extract_text() or ""
            characters += len(part)
            if characters > MAX_DOCUMENT_CHARACTERS:
                raise ValueError("Extracted document is too large")
            parts.append(part)
        result = "\n".join(parts)
    elif extension in {".txt", ".md", ".markdown", ".html", ".htm"}:
        result = data.decode("utf-8", errors="replace")
        if extension in {".html", ".htm"}:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(result, "html.parser")
            for node in soup(["script", "style", "noscript"]):
                node.decompose()
            result = soup.get_text(" ", strip=True)
    else:
        raise ValueError("Supported formats: txt, md, html, pdf")
    if len(result) > MAX_DOCUMENT_CHARACTERS:
        raise ValueError("Extracted document is too large")
    return result


def reciprocal_rank_fusion(
    rankings: list[list[str]], limit: int = 5, k: int = 60
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, identifier in enumerate(ranking, 1):
            scores[identifier] = scores.get(identifier, 0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


class RAGService:
    def __init__(
        self,
        session: Any,
        gateway: Any,
        reranker: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    ):
        self.session = session
        self.gateway = gateway
        self.reranker = reranker

    def _span(
        self, project_id: str, kind: str, name: str, started: float,
        inputs: dict[str, Any], outputs: dict[str, Any],
    ) -> None:
        from si.db import Span

        self.session.add(
            Span(
                project_id=project_id, kind=kind, name=name, inputs=inputs, outputs=outputs,
                latency_ms=(time.monotonic() - started) * 1000,
            )
        )

    async def _embed(self, project_id: str, texts: list[str]) -> list[list[float]]:
        started = time.monotonic()
        vectors = await self.gateway.embed(texts)
        if not getattr(self.gateway, "records_embedding_spans", False):
            self._span(
                project_id, "embedding", "rag_embedding", started,
                {"count": len(texts)}, {"dimensions": 64, "count": len(vectors)},
            )
        return vectors

    def _collections(self, project_id: str, collection_ids: list[str]) -> None:
        from si.db import Collection

        if not collection_ids:
            return
        found = set(
            self.session.scalars(
                select(Collection.id).where(
                    Collection.project_id == project_id, Collection.id.in_(collection_ids)
                )
            )
        )
        if found != set(collection_ids):
            raise ValueError("Collection not found in project")

    async def ingest(
        self, project_id: str, collection_id: str, filename: str, data: bytes
    ) -> int:
        from si.db import Chunk

        started = time.monotonic()
        self._collections(project_id, [collection_id])
        pieces = chunk_text(parse_document(filename, data))
        # Bound embedding batches, not one provider request per document.
        staged = []
        for start in range(0, len(pieces), 64):
            batch = pieces[start : start + 64]
            vectors = await self._embed(project_id, batch)
            if len(vectors) != len(batch):
                raise ValueError("Embedding count does not match input")
            for piece, vector in zip(batch, vectors, strict=True):
                self._validate_vector(vector)
                staged.append(
                    Chunk(
                        project_id=project_id,
                        collection_id=collection_id,
                        text=piece,
                        embedding=vector,
                    )
                )
        self.session.add_all(staged)
        self._span(
            project_id, "ingestion", filename[:200], started,
            {"collection_id": collection_id, "bytes": len(data)}, {"chunks": len(staged)},
        )
        self.session.commit()
        return len(staged)

    async def ingest_url(self, project_id: str, collection_id: str, url: str) -> int:
        self._collections(project_id, [collection_id])
        data, content_type = await safe_fetch(url, max_bytes=MAX_DOCUMENT_BYTES)
        filename = "document.pdf" if "application/pdf" in content_type else "document.html"
        if content_type.startswith("text/plain"):
            filename = "document.txt"
        return await self.ingest(project_id, collection_id, filename, data)

    @staticmethod
    def _validate_vector(vector: list[float]) -> None:
        if len(vector) != 64 or not all(math.isfinite(value) for value in vector):
            raise ValueError("Expected a finite 64-dimensional embedding")

    async def search(
        self,
        project_id: str,
        collection_ids: list[str],
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        from si.db import Chunk

        started = time.monotonic()
        if not 1 <= limit <= 100 or not query.strip() or len(query) > 20_000:
            raise ValueError("Invalid search query or limit")
        self._collections(project_id, collection_ids)
        if not collection_ids:
            return []
        vectors = await self._embed(project_id, [query])
        if len(vectors) != 1:
            raise ValueError("Expected one query embedding")
        vector = vectors[0]
        self._validate_vector(vector)
        candidates = max(limit * 4, 20)
        if self.session.get_bind().dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector
            # Separate indexed rankings; combine with RRF rather than adding
            # incomparable cosine and full-text rank scores.
            semantic = self.session.scalars(
                select(Chunk)
                .where(
                    Chunk.project_id == project_id,
                    Chunk.collection_id.in_(collection_ids),
                )
                .order_by(cast(Chunk.embedding, Vector(64)).cosine_distance(vector))
                .limit(candidates)
            ).all()
            from sqlalchemy import func

            tsquery = func.plainto_tsquery("english", query)
            document = func.to_tsvector("english", Chunk.text)
            lexical = self.session.scalars(
                select(Chunk)
                .where(
                    Chunk.project_id == project_id,
                    Chunk.collection_id.in_(collection_ids),
                    document.op("@@")(tsquery),
                )
                .order_by(func.ts_rank_cd(document, tsquery).desc())
                .limit(candidates)
            ).all()
        else:
            # SQLite's development fallback uses real cosine and term matching;
            # it does not pretend to execute PostgreSQL full-text/vector SQL.
            rows = self.session.scalars(
                select(Chunk).where(
                    Chunk.project_id == project_id,
                    Chunk.collection_id.in_(collection_ids),
                ).limit(50_001)
            ).all()
            if len(rows) > 50_000:
                raise ValueError("SQLite retrieval is limited to 50000 chunks; use PostgreSQL")
            norm = math.sqrt(sum(v * v for v in vector))

            def cosine(row: Any) -> float:
                other = math.sqrt(sum(v * v for v in row.embedding))
                return (
                    sum(a * b for a, b in zip(vector, row.embedding, strict=True)) / (norm * other)
                    if norm and other
                    else 0
                )

            terms = set(query.lower().split())

            def lexical_score(row: Any) -> int:
                return sum(row.text.lower().count(term) for term in terms)

            semantic = sorted(rows, key=cosine, reverse=True)[:candidates]
            lexical = sorted(
                (row for row in rows if lexical_score(row)), key=lexical_score, reverse=True
            )[:candidates]
        by_id = {row.id: row for row in [*semantic, *lexical]}
        ranked = reciprocal_rank_fusion(
            [[row.id for row in semantic], [row.id for row in lexical]], limit=candidates
        )
        results = [
            {"id": identifier, "text": by_id[identifier].text, "score": score}
            for identifier, score in ranked
        ]
        if self.reranker:
            results = self.reranker(query, results)
        results = results[:limit]
        self._span(
            project_id, "retrieval", "hybrid_search", started,
            {"collection_ids": collection_ids, "query": query, "limit": limit},
            {"matches": [{"id": row["id"], "score": row["score"]} for row in results]},
        )
        self.session.commit()
        return results


async def ingest(
    session: Any, project_id: str, collection_id: str, filename: str, data: bytes,
    gateway: Any = None, redis: Any = None,
) -> dict[str, int]:
    if gateway is None:
        from si.agents.runtime import ProjectGateway

        gateway = ProjectGateway(session, project_id, redis)
    count = await RAGService(session, gateway).ingest(project_id, collection_id, filename, data)
    return {"chunks": count}


async def search(
    session: Any, project_id: str, collection_id: str, query: str, limit: int = 5,
    gateway: Any = None, redis: Any = None,
) -> list[dict[str, Any]]:
    if gateway is None:
        from si.agents.runtime import ProjectGateway

        gateway = ProjectGateway(session, project_id, redis)
    return await RAGService(session, gateway).search(project_id, [collection_id], query, limit)
