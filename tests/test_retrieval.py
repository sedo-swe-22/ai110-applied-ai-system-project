"""Tests for retrieval (RAG): chunking, BM25 ranking, filters, history source, citations.

No LLM and no API key: retrieval is plain Python.
"""

from datetime import date

import pytest

import retrieval
from agent_tools import make_ref
from guardrails import DIAGNOSIS, DOSAGE, EMERGENCY, GIVE_MED, SYMPTOM
from pawpal_system import Owner, Pet, Task
from retrieval import (
    BM25Index,
    Chunk,
    MultiSourceRetriever,
    build_retriever,
    chunk_document,
    find_citations,
    guideline_index,
    history_chunks,
    load_guideline_chunks,
    normalize_cite,
    normalize_life_stage,
    normalize_species,
    stem,
    tokenize,
)


def doc(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- text handling

@pytest.mark.parametrize("word, expected", [
    ("puppies", "puppy"), ("walks", "walk"), ("feeding", "feed"), ("grooming", "groom"),
    ("pup", "puppy"), ("kitty", "kitten"), ("meals", "feed"), ("class", "class"),
])
def test_stem_and_synonyms(word, expected):
    assert stem(word) == expected


def test_tokenize_drops_stop_words_and_punctuation():
    assert tokenize("How should I feed my Puppies?") == ["feed", "puppy"]


@pytest.mark.parametrize("raw, species", [
    ("Puppy", "dog"), ("dogs", "dog"), ("Kitten", "cat"), ("guinea pig", "small_pet"),
    ("hamster", "small_pet"), ("axolotl", "axolotl"),
])
def test_normalize_species(raw, species):
    assert normalize_species(raw) == species


def test_normalize_life_stage():
    assert normalize_life_stage("Elderly") == "senior"
    assert normalize_life_stage("pup") == "puppy"


# ---------------------------------------------------------------- chunking

def test_chunk_document_splits_on_sections_and_reads_front_matter(tmp_path):
    path = doc(tmp_path, "dog-test.md", (
        "---\nspecies: dog\nlife_stage: puppy\n---\n# Test title\n\n> A disclaimer.\n\n"
        "## Feeding\nThree meals a day.\n\n## Walks\nFour short walks.\n"))
    chunks = chunk_document(path)
    assert [c.heading for c in chunks] == ["Feeding", "Walks"]
    assert chunks[0].cite == "[dog-test.md: Feeding]"
    assert chunks[0].meta == {"species": "dog", "life_stage": "puppy"}
    assert chunks[0].title == "Test title" and chunks[0].text == "Three meals a day."
    assert all("disclaimer" not in c.text for c in chunks)  # the preamble is not indexed


def test_missing_front_matter_means_any(tmp_path):
    chunks = chunk_document(doc(tmp_path, "x.md", "# T\n\n## A\nsome text\n"))
    assert chunks[0].meta == {"species": "any", "life_stage": "any"}


def test_real_knowledge_base_loads_with_unique_citations():
    chunks = load_guideline_chunks()
    assert len(chunks) >= 25
    assert len({c.cite for c in chunks}) == len(chunks)
    assert {c.meta["species"] for c in chunks} >= {"dog", "cat", "small_pet", "any"}


def test_knowledge_docs_contain_no_medical_advice():
    """The guidelines are about scheduling only. The medical guardrail patterns must not match."""
    for chunk in load_guideline_chunks():
        text = f"{chunk.heading}. {chunk.text}"
        for name, pattern in [("emergency", EMERGENCY), ("dosage", DOSAGE), ("give-med", GIVE_MED),
                              ("diagnosis", DIAGNOSIS), ("symptom", SYMPTOM)]:
            assert not pattern.search(text), f"{chunk.cite} matches the {name} guardrail pattern"


# ---------------------------------------------------------------- ranking

@pytest.fixture(scope="module")
def guides():
    return guideline_index()


def top_cites(hits):
    return [h.cite for h in hits]


def test_puppy_feeding_query_finds_the_puppy_section(guides):
    hits = guides.search("how often should I feed my pup", k=3)
    assert hits[0].cite == "[dog-puppy.md: Feeding]"


def test_senior_walks_query(guides):
    assert guides.search("senior dog walks")[0].cite == "[dog-senior.md: Walks]"


def test_filters_keep_matching_and_generic_chunks(guides):
    hits = guides.search("daily routine feeding walks play", k=20, filters={"species": "dog", "life_stage": "puppy"})
    assert hits
    for h in hits:
        assert h.chunk.meta["species"] in {"dog", "any"}
        assert h.chunk.meta["life_stage"] in {"puppy", "any"}


def test_exact_filter_match_outranks_generic_sections(guides):
    hits = guides.search("puppy daily routine feeding walks play", k=3,
                         filters={"species": "dog", "life_stage": "puppy"})
    assert all(h.chunk.meta["species"] == "dog" for h in hits)


def test_unknown_species_only_matches_generic_documents(guides):
    hits = guides.search("daily routine", k=10, filters={"species": "axolotl"})
    assert hits and all(h.chunk.meta["species"] == "any" for h in hits)


def test_unrelated_query_finds_nothing(guides):
    assert guides.search("quantum physics homework") == []
    assert guides.search("") == []


def test_hits_are_ranked_and_carry_scores(guides):
    hits = guides.search("kitten feeding play litter", k=5)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0].relative == 1.0 and all(0 < h.relative <= 1 for h in hits)


def test_weak_matches_are_cut_off():
    index = BM25Index([
        Chunk("guidelines", "[a.md: Strong]", "A", "Strong", "puppy puppy puppy feeding feeding"),
        Chunk("guidelines", "[a.md: Weak]", "A", "Weak", "feeding " + "filler " * 40),
    ])
    assert top_cites(index.search("puppy feeding", k=5)) == ["[a.md: Strong]"]


# ---------------------------------------------------------------- history source

@pytest.fixture
def owner():
    o = Owner("Jordan")
    max_ = Pet("Max", "Beagle")
    mochi = Pet("Mochi", "Shiba Inu")
    o.add_pet(max_)
    o.add_pet(mochi)
    max_.add_task(Task("Morning walk", "08:00", frequency="daily", due_date=date(2026, 11, 10)))
    max_.add_task(Task("Evening meds", "20:00", frequency="daily", due_date=date(2026, 11, 10)))
    mochi.add_task(Task("Breakfast", "07:30", frequency="daily", due_date=date(2026, 11, 10)))
    return o


def test_history_refs_match_the_tool_layer(owner):
    cites = {c.cite for c in history_chunks(owner)}
    expected = {f"[history: {make_ref(p, i)}]" for p in owner.pets for i in range(len(p.tasks))}
    assert cites == expected


def test_history_search_by_pet_and_word(owner):
    index = BM25Index(history_chunks(owner))
    assert top_cites(index.search("Mochi breakfast")) == ["[history: Mochi:0]"]
    only_max = index.search("walk", filters={"pet": "Max"})
    assert top_cites(only_max) == ["[history: Max:0]"]
    assert index.search("walk", filters={"pet": "Mochi"}) == []


def test_history_is_rebuilt_from_live_data(owner):
    before = build_retriever(owner).search("grooming", sources=["history"])
    owner.pets[1].add_task(Task("Grooming", "10:00", due_date=date(2026, 11, 11)))
    after = build_retriever(owner).search("grooming", sources=["history"])
    assert before == [] and top_cites(after) == ["[history: Mochi:1]"]


# ---------------------------------------------------------------- several sources

def test_multi_source_search_and_source_selection(owner):
    r = build_retriever(owner)
    only_history = r.search("walk", sources=["history"])
    assert only_history and all(h.chunk.source == "history" for h in only_history)
    only_guides = r.search("walk", sources=["guidelines"])
    assert only_guides and all(h.chunk.source == "guidelines" for h in only_guides)
    both = r.search("Max walk daily", k=6)
    assert {h.chunk.source for h in both} == {"history", "guidelines"}


def test_unknown_source_is_an_error(owner):
    with pytest.raises(ValueError, match="unknown source"):
        build_retriever(owner).search("walk", sources=["nope"])


def test_multi_source_merges_by_relative_score():
    a = BM25Index([Chunk("a", "[a.md: One]", "A", "One", "puppy puppy feeding")])
    b = BM25Index([Chunk("b", "[b.md: Two]", "B", "Two", "puppy puppy feeding")])
    hits = MultiSourceRetriever({"a": a, "b": b}).search("puppy feeding", k=2)
    assert len(hits) == 2 and all(h.relative == 1.0 for h in hits)


# ---------------------------------------------------------------- citations

def test_find_and_normalize_citations():
    text = "Fed 3 times [dog-puppy.md: Feeding] and copied [history: Max:0]. Not a cite: [Max:0] [1]."
    assert find_citations(text) == ["[dog-puppy.md: Feeding]", "[history: Max:0]"]
    assert normalize_cite("[Dog-Puppy.md:  Feeding]") == normalize_cite("[dog-puppy.md: feeding]")
