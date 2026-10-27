"""Local reference retrieval, separate from authoritative policy and business services."""

from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class SourceMetadata(KnowledgeModel):
    document_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,80}$")]
    title: Label
    version: Label
    source_uri: Annotated[str, StringConstraints(pattern=r"^local-knowledge://[a-z0-9-]{1,80}$")]
    authority: Literal["reference_only"] = "reference_only"


class KnowledgeDocument(KnowledgeModel):
    source: SourceMetadata
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]


class KnowledgeChunk(KnowledgeModel):
    chunk_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    source: SourceMetadata
    text: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    start_char: int = Field(strict=True, ge=0)
    end_char: int = Field(strict=True, gt=0)
    trust: Literal["untrusted_information"] = "untrusted_information"

    @model_validator(mode="after")
    def validate_span(self):
        if self.end_char - self.start_char != len(self.text) or not self.text.strip():
            raise ValueError("Invalid chunk span or blank text")
        return self


class KnowledgeQuery(KnowledgeModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    top_k: int = Field(default=3, strict=True, ge=1, le=5)

    @model_validator(mode="after")
    def has_terms(self):
        if not re.search(r"\w", self.query, re.UNICODE):
            raise ValueError("Query must contain a searchable term")
        return self


class RetrievalHit(KnowledgeModel):
    chunk: KnowledgeChunk
    score: float = Field(gt=0, le=1, allow_inf_nan=False)


class RetrievalResult(KnowledgeModel):
    query: KnowledgeQuery
    hits: tuple[RetrievalHit, ...] = Field(max_length=5)
    method: Literal["local_token_cosine_v1"] = "local_token_cosine_v1"
    trust: Literal["untrusted_information"] = "untrusted_information"
    usage: Literal["Reference evidence only; deterministic rules and human review remain authoritative."] = (
        "Reference evidence only; deterministic rules and human review remain authoritative."
    )

    @model_validator(mode="after")
    def validate_hits(self):
        ids = [hit.chunk.chunk_id for hit in self.hits]
        if len(ids) > self.query.top_k or len(set(ids)) != len(ids):
            raise ValueError("Invalid retrieval count or duplicate chunk")
        return self


class KnowledgeError(Exception):
    """Safe local retrieval failure; no paths or raw source errors exposed."""


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> dict[str, float]: ...


class KnowledgeRetriever(Protocol):
    def search(self, request: KnowledgeQuery) -> RetrievalResult: ...


class TokenEmbedding:
    """Deterministic sparse bag-of-words features, not a semantic neural embedding."""

    def embed(self, text: str) -> dict[str, float]:
        counts = Counter(re.findall(r"\w+", text.casefold(), re.UNICODE))
        norm = math.sqrt(sum(count * count for count in counts.values()))
        return {term: count / norm for term, count in sorted(counts.items())} if norm else {}


DEFAULT_CORPUS = Path(__file__).resolve().parent.parent / "knowledge" / "support.json"


def load_documents(path: Path) -> tuple[KnowledgeDocument, ...]:
    try:
        with path.open("rb") as source:
            raw = source.read(262145)
        if len(raw) > 262144:
            raise ValueError("Corpus too large")
        values = json.loads(raw.decode("utf-8"))
        if not isinstance(values, list) or not 1 <= len(values) <= 16:
            raise ValueError("Corpus must contain 1 to 16 documents")
        documents = tuple(KnowledgeDocument.model_validate(value) for value in values)
        documents = tuple(KnowledgeDocument(source=d.source, text=d.text.replace("\r\n", "\n").replace("\r", "\n"))
                          for d in documents)
        ids = [d.source.document_id for d in documents]
        uris = [d.source.source_uri for d in documents]
        if len(set(ids)) != len(ids) or len(set(uris)) != len(uris):
            raise ValueError("Duplicate sources")
        return tuple(sorted(documents, key=lambda d: d.source.document_id))
    except Exception:
        raise KnowledgeError("Knowledge corpus is unavailable or invalid") from None


def chunk_document(document: KnowledgeDocument) -> tuple[KnowledgeChunk, ...]:
    document = KnowledgeDocument.model_validate(document)
    chunks = []
    for start in range(0, len(document.text), 600):
        text = document.text[start:start + 600]
        if not text.strip():
            continue
        identity = json.dumps([document.source.model_dump(), start, text], sort_keys=True, ensure_ascii=False)
        chunks.append(KnowledgeChunk(chunk_id=sha256(identity.encode("utf-8")).hexdigest(),
            source=document.source, text=text, start_char=start, end_char=start + len(text)))
    return tuple(chunks)


class LocalKnowledgeRetriever:
    def __init__(self, path: Path = DEFAULT_CORPUS, embedding: EmbeddingProvider | None = None):
        self._path = path
        self._embedding = embedding or TokenEmbedding()

    def _vector(self, text: str) -> dict[str, float]:
        vector = self._embedding.embed(text)
        if not isinstance(vector, dict) or any(
            not isinstance(key, str) or type(value) not in (int, float)
            or not math.isfinite(value) or value < 0 for key, value in vector.items()
        ):
            raise KnowledgeError("Invalid embedding output")
        norm = math.sqrt(sum(value * value for value in vector.values()))
        if not math.isfinite(norm):
            raise KnowledgeError("Invalid embedding output")
        return {key: value / norm for key, value in vector.items()} if norm else {}

    def search(self, request: KnowledgeQuery) -> RetrievalResult:
        request = KnowledgeQuery.model_validate(request)
        try:
            documents = load_documents(self._path)
            chunks = tuple(chunk for d in documents for chunk in chunk_document(d))
            if not chunks:
                raise KnowledgeError("Empty knowledge")
            query = self._vector(request.query)
            hits = []
            for chunk in chunks:
                vector = self._vector(chunk.source.title + "\n" + chunk.text)
                score = min(1.0, sum(value * vector.get(term, 0) for term, value in query.items()))
                if score > 0:
                    hits.append(RetrievalHit(chunk=chunk, score=score))
            hits.sort(key=lambda h: (-h.score, h.chunk.source.document_id, h.chunk.start_char))
            return RetrievalResult(query=request, hits=tuple(hits[:request.top_k]))
        except Exception:
            raise KnowledgeError("Knowledge retrieval is unavailable") from None


def retrieve_safely(retriever: KnowledgeRetriever, request: KnowledgeQuery) -> RetrievalResult:
    try:
        result = RetrievalResult.model_validate(retriever.search(request))
        if result.query != request:
            raise ValueError("Mismatched query")
        return result
    except Exception:
        raise KnowledgeError("Knowledge retrieval is unavailable") from None
