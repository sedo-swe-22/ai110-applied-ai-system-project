"""Retrieval for the PawPal+ agent (RAG).

Two sources sit behind one small interface, so the agent can look up facts
before it plans:

* **guidelines**: the markdown files in `knowledge/`. Each `##` section is one
  chunk, and the front matter (`species:`, `life_stage:`) lets a search be
  narrowed to, say, puppies.
* **history**: the owner's own tasks (one chunk per task), rebuilt on every
  call from the live data, so it is never out of date.

Search is BM25 over lowercase, lightly stemmed words. It is pure Python: no
extra dependency, no API call, and the same answer every time, so tests and
evals do not need a key or any quota.

Every hit carries a citation string such as `[dog-puppy.md: Feeding]` or
`[history: Max:1]`. The agent must copy it into its reply, and the output
guardrail checks that a cited source was really retrieved.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Optional, Protocol

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

GUIDELINES = "guidelines"
HISTORY = "history"

# BM25 settings and cut-offs. A hit must score at least MIN_SCORE, and at least
# RELATIVE_CUTOFF of the best hit, so weak matches come back as "nothing found"
# instead of a confident-looking wrong answer.
K1 = 1.5
B = 0.75
MIN_SCORE = 0.6
RELATIVE_CUTOFF = 0.4
# A chunk written for exactly the filtered species / life stage beats a generic one.
EXACT_FILTER_BONUS = 0.25

STOP_WORDS = frozenset(
    "a an the and or but for of to in on at by with from as is are was were be it its this that "
    "my me i we you your our can could should would do does did how what when which who please "
    "up about into".split()
)
# Words people use for the same thing, mapped to the word the documents use.
SYNONYMS = {
    "pup": "puppy",
    "kitty": "kitten",
    "elderly": "senior",
    "older": "senior",
    "bunny": "rabbit",
    "meal": "feed",
    "food": "feed",
    "eat": "feed",
}

_SPECIES = {
    "dog": "dog", "dogs": "dog", "puppy": "dog", "puppies": "dog", "pup": "dog",
    "cat": "cat", "cats": "cat", "kitten": "cat", "kittens": "cat", "kitty": "cat",
    "rabbit": "small_pet", "bunny": "small_pet", "guinea pig": "small_pet",
    "hamster": "small_pet", "small pet": "small_pet", "small_pet": "small_pet",
}
_LIFE_STAGES = {
    "puppy": "puppy", "pup": "puppy", "kitten": "kitten", "kitty": "kitten", "baby": "puppy",
    "adult": "adult", "grown": "adult",
    "senior": "senior", "elderly": "senior", "older": "senior", "old": "senior",
}


def normalize_species(value: str) -> str:
    """Map what the user or model said ("Puppy", "guinea pig") to a document species key.

    An unknown species stays as typed, so it matches only the generic documents.
    """
    key = (value or "").strip().casefold().replace("-", " ")
    return _SPECIES.get(key, key)


def normalize_life_stage(value: str) -> str:
    key = (value or "").strip().casefold()
    return _LIFE_STAGES.get(key, key)


def stem(word: str) -> str:
    """A very light stemmer: puppies -> puppy, walks -> walk, feeding -> feed."""
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    return SYNONYMS.get(word, word)


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").casefold())
    return [stem(w) for w in words if w not in STOP_WORDS]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Chunk:
    source: str  # GUIDELINES or HISTORY
    cite: str  # what the reply must quote, e.g. "[dog-puppy.md: Feeding]"
    title: str
    heading: str
    text: str
    meta: Mapping[str, str] = field(default_factory=dict)

    def index_text(self) -> str:
        """What gets indexed: the heading counts double, the metadata once."""
        meta = " ".join(str(v) for v in self.meta.values())
        return f"{self.title} {self.heading} {self.heading} {meta} {self.text}"


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float  # raw BM25
    relative: float = 1.0  # score / best score from the same source

    @property
    def cite(self) -> str:
        return self.chunk.cite


class Retriever(Protocol):
    def search(self, query: str, k: int = 3, filters: Optional[Mapping[str, str]] = None) -> list[Hit]: ...


# ---------------------------------------------------------------------------
# BM25 index
# ---------------------------------------------------------------------------

class BM25Index:
    """BM25 over a fixed list of chunks."""

    def __init__(self, chunks: Iterable[Chunk]) -> None:
        self.chunks = list(chunks)
        self._terms = [Counter(tokenize(c.index_text())) for c in self.chunks]
        self._lengths = [sum(t.values()) for t in self._terms]
        n = len(self.chunks)
        self._avg_len = (sum(self._lengths) / n) if n else 0.0
        df: Counter[str] = Counter()
        for terms in self._terms:
            df.update(terms.keys())
        # The "+1" form keeps every idf positive, even for words found in most chunks.
        self._idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    def __len__(self) -> int:
        return len(self.chunks)

    @staticmethod
    def _filter_match(chunk: Chunk, filters: Optional[Mapping[str, str]]) -> Optional[int]:
        """None if the chunk is filtered out, else how many filters it matches exactly.

        A filter keeps chunks whose metadata matches, plus chunks marked "any".
        """
        exact = 0
        for key, wanted in (filters or {}).items():
            have = str(chunk.meta.get(key, "any")).casefold()
            if have == str(wanted).casefold():
                exact += 1
            elif have != "any":
                return None
        return exact

    def search(self, query: str, k: int = 3, filters: Optional[Mapping[str, str]] = None) -> list[Hit]:
        words = list(dict.fromkeys(tokenize(query)))  # unique, in order
        if not words or not self.chunks:
            return []
        scored: list[tuple[float, int]] = []
        for i, chunk in enumerate(self.chunks):
            exact = self._filter_match(chunk, filters)
            if exact is None:
                continue
            terms, length = self._terms[i], self._lengths[i]
            score = 0.0
            for w in words:
                tf = terms.get(w, 0)
                if not tf:
                    continue
                norm = tf + K1 * (1 - B + B * length / (self._avg_len or 1))
                score += self._idf[w] * tf * (K1 + 1) / norm
            score *= 1 + EXACT_FILTER_BONUS * exact
            if score >= MIN_SCORE:
                scored.append((score, i))
        scored.sort(key=lambda s: (-s[0], s[1]))
        if not scored:
            return []
        best = scored[0][0]
        return [
            Hit(self.chunks[i], round(score, 3), round(score / best, 3))
            for score, i in scored[:k]
            if score / best >= RELATIVE_CUTOFF
        ]


# ---------------------------------------------------------------------------
# Source 1: guideline documents
# ---------------------------------------------------------------------------

def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    meta = {}
    for line in raw[3:end].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, raw[end + 4:].lstrip("\n")


def chunk_document(path: Path) -> list[Chunk]:
    """Split one markdown file into one chunk per `##` section."""
    meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
    meta = {"species": meta.get("species", "any"), "life_stage": meta.get("life_stage", "any")}
    title = path.stem
    sections: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        # Text before the first "##" (title, disclaimer) is not indexed on its own.
    chunks = []
    for heading, lines in sections:
        text = " ".join(l.strip() for l in lines if l.strip())
        if text:
            chunks.append(Chunk(GUIDELINES, f"[{path.name}: {heading}]", title, heading, text, meta))
    return chunks


def load_guideline_chunks(directory: Path = KNOWLEDGE_DIR) -> list[Chunk]:
    return [c for path in sorted(Path(directory).glob("*.md")) for c in chunk_document(path)]


@lru_cache(maxsize=4)
def guideline_index(directory: str = str(KNOWLEDGE_DIR)) -> BM25Index:
    """Built once per process: the documents do not change while the app runs."""
    return BM25Index(load_guideline_chunks(Path(directory)))


# ---------------------------------------------------------------------------
# Source 2: the owner's own task history
# ---------------------------------------------------------------------------

def history_chunks(owner) -> list[Chunk]:
    """One chunk per task. The ref ("Max:1") matches agent_tools.make_ref."""
    chunks = []
    for pet in owner.pets:
        for i, task in enumerate(pet.tasks):
            ref = f"{pet.name}:{i}"
            state = "done" if task.completed else "open"
            text = (f"{pet.name} the {pet.breed}: {task.description} at {task.time}, repeats {task.frequency}, "
                    f"priority {task.priority}, due {task.due_date.isoformat()}, {state}.")
            chunks.append(Chunk(HISTORY, f"[history: {ref}]", f"{pet.name}'s tasks", task.description, text,
                                {"pet": pet.name}))
    return chunks


# ---------------------------------------------------------------------------
# Several sources behind one search
# ---------------------------------------------------------------------------

class MultiSourceRetriever:
    """Search one or more named sources and merge the hits by relative score."""

    def __init__(self, sources: Mapping[str, Retriever]) -> None:
        self.sources = dict(sources)

    def search(
        self,
        query: str,
        k: int = 3,
        sources: Optional[Iterable[str]] = None,
        filters: Optional[Mapping[str, str]] = None,
    ) -> list[Hit]:
        names = list(sources) if sources is not None else list(self.sources)
        unknown = [n for n in names if n not in self.sources]
        if unknown:
            raise ValueError(f"unknown source(s): {', '.join(unknown)}")
        merged: list[Hit] = []
        for name in names:
            merged.extend(self.sources[name].search(query, k=k, filters=filters))
        merged.sort(key=lambda h: (-h.relative, -h.score, h.cite))
        return merged[:k]


def build_retriever(owner, knowledge_dir: Path = KNOWLEDGE_DIR) -> MultiSourceRetriever:
    """The retriever the agent tools use: cached guidelines + the owner's live history."""
    return MultiSourceRetriever({
        GUIDELINES: guideline_index(str(knowledge_dir)),
        HISTORY: BM25Index(history_chunks(owner)),
    })


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

_CITATION = re.compile(r"\[(?:[\w.-]+\.md: [^\]]+|history: [^\]]+)\]")


def normalize_cite(cite: str) -> str:
    return re.sub(r"\s+", " ", cite).strip().casefold()


def find_citations(text: str) -> list[str]:
    """Citation-shaped markers in a reply, e.g. "[dog-puppy.md: Feeding]"."""
    return _CITATION.findall(text or "")
