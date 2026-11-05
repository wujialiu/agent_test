"""Build a small vector index and expose the same retrieval contract in both modes."""

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from stage_01_model_tools_py.model_py import API
from stage_01_model_tools_py.transport_py import Http, ProviderFormatError

DEFAULT_CORPUS = Path(__file__).with_name("corpus")
MAX_DOCUMENTS = 50
MAX_DOCUMENT_BYTES = 80_000
MAX_CHUNKS = 500
WORD = re.compile(r"[\w-]+", re.UNICODE)
STOP_WORDS = set(
    "a an and are can does for how is it of or should that the to we what when why with".split()
)


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    text: str


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


def load_chunks(root: Path, size: int = 100, overlap: int = 20) -> list[Chunk]:
    if not 0 <= overlap < size:
        raise ValueError("chunk overlap must be smaller than the positive chunk size")
    files = sorted(root.glob("*.md"))
    if not 1 <= len(files) <= MAX_DOCUMENTS:
        raise ValueError(f"corpus must contain 1 to {MAX_DOCUMENTS} Markdown files")
    chunks = []
    for path in files:
        if path.is_symlink():
            raise ValueError("symlinks are excluded from the corpus")
        with path.open("rb") as file:
            raw = file.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"corpus document is too large: {path.name}")
        words = re.sub(r"(?m)^#{1,6} .*$", "", raw.decode()).split()
        for start in range(0, len(words), size - overlap):
            end = start + size
            text = " ".join(words[start:end])
            digest = hashlib.sha256(f"{path.name}\0{start}\0{text}".encode()).hexdigest()
            chunks.append(Chunk(digest[:16], path.name, text))
            if len(chunks) > MAX_CHUNKS:
                raise ValueError("corpus exceeded the maximum chunk count")
            # Stop as soon as the last full or partial window reaches EOF.
            if end >= len(words):
                break
    if not chunks:
        raise ValueError("corpus is empty")
    return chunks


class Embedder(Protocol):
    namespace: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashEmbedder:
    """A deterministic offline baseline. Token overlap drives the similarity."""

    namespace = "hash-v1"
    dimensions = 256

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]

    def vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in WORD.findall(text.casefold()):
            if word in STOP_WORDS:
                continue
            # Python's built-in hash changes across runs. Persisted vectors need
            # a stable feature mapping, so derive a bucket and sign from SHA-256.
            digest = hashlib.sha256(word.encode()).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[bucket] += 1 if digest[4] % 2 else -1
        magnitude = math.sqrt(sum(value * value for value in vector))
        return [value / magnitude for value in vector] if magnitude else vector


class OpenAIEmbedder:
    namespace = "text-embedding-3-small"

    def __init__(self, http: Http, api_key: str):
        if not api_key.strip():
            raise ValueError("set OPENAI_API_KEY for live embeddings")
        self.http = http
        self.api_key = api_key

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self.http.json(
            "OpenAI embeddings",
            "POST",
            f"{API}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.namespace, "input": list(texts), "encoding_format": "float"},
            retry=True,
        )
        try:
            rows = sorted(data["data"], key=lambda item: item["index"])
            if [row["index"] for row in rows] != list(range(len(texts))):
                raise ValueError("embedding indexes do not match requested texts")
            vectors = [[float(value) for value in row["embedding"]] for row in rows]
            if not vectors[0] or any(
                len(vector) != len(vectors[0]) or not all(map(math.isfinite, vector))
                for vector in vectors
            ):
                raise ValueError("invalid embedding vector")
            return vectors
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ProviderFormatError(
                "embedding response fields did not match the contract"
            ) from None


class Index(Protocol):
    async def search(self, query: str, top_k: int) -> list[Hit]: ...


def validate_search(query: str, top_k: int) -> None:
    if not query.strip() or len(query) > 4000 or not 1 <= top_k <= 12:
        raise ValueError("provide a query up to 4000 characters and top_k between 1 and 12")


class MemoryIndex:
    def __init__(self, chunks: list[Chunk], vectors: list[list[float]], embedder: HashEmbedder):
        self.rows = list(zip(chunks, vectors, strict=True))
        self.embedder = embedder

    @classmethod
    async def build(cls, chunks: list[Chunk]) -> "MemoryIndex":
        embedder = HashEmbedder()
        return cls(chunks, await embedder.embed([chunk.text for chunk in chunks]), embedder)

    async def search(self, query: str, top_k: int) -> list[Hit]:
        validate_search(query, top_k)
        vector = self.embedder.vector(query)
        hits = [
            Hit(chunk, sum(x * y for x, y in zip(vector, stored, strict=True)))
            for chunk, stored in self.rows
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.chunk.id))
        return [hit for hit in hits[:top_k] if hit.score >= 0.15]


class ChromaIndex:
    """Use our embedder explicitly so Chroma never downloads an implicit model."""

    def __init__(self, collection, embedder: Embedder):
        self.collection = collection
        self.embedder = embedder

    @classmethod
    async def build(cls, path: Path, chunks: list[Chunk], embedder: Embedder) -> "ChromaIndex":
        import chromadb

        digest = hashlib.sha256(
            (embedder.namespace + ":" + ":".join(chunk.id for chunk in chunks)).encode()
        ).hexdigest()[:24]
        # A new corpus or embedder gets a new collection. Old chunks cannot leak
        # into results after editing a source or changing the vector dimensions.
        client = await asyncio.to_thread(
            chromadb.PersistentClient,
            path=str(path),
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        collection = await asyncio.to_thread(
            client.get_or_create_collection,
            f"corpus-{digest}",
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
        )
        if await asyncio.to_thread(collection.count) != len(chunks):
            vectors = await embedder.embed([chunk.text for chunk in chunks])
            await asyncio.to_thread(
                collection.upsert,
                ids=[chunk.id for chunk in chunks],
                documents=[chunk.text for chunk in chunks],
                metadatas=[{"source": chunk.source} for chunk in chunks],
                embeddings=vectors,
            )
        return cls(collection, embedder)

    async def search(self, query: str, top_k: int) -> list[Hit]:
        validate_search(query, top_k)
        vectors = await self.embedder.embed([query])
        count = await asyncio.to_thread(self.collection.count)
        if not count:
            return []
        data = await asyncio.to_thread(
            self.collection.query,
            query_embeddings=vectors,
            n_results=min(top_k, count),
            include=["documents", "metadatas", "distances"],
        )
        cutoff = 0.15 if isinstance(self.embedder, HashEmbedder) else 0.25
        return [
            Hit(Chunk(chunk_id, meta["source"], text), 1 - distance)
            for chunk_id, text, meta, distance in zip(
                data["ids"][0],
                data["documents"][0],
                data["metadatas"][0],
                data["distances"][0],
                strict=True,
            )
            if 1 - distance >= cutoff
        ]
