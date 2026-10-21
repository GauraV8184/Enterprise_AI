"""The specialist agents: SQL, web research, and report writing.

The store, embeddings, chunking, retrieval and SQL execution are real; the chat
model and the search provider are faked. That split is what makes these tests
worth reading: everything except the two network-bound services behaves exactly
as it does in production, including the repair loop and the "no evidence, no
answer" rule.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.agents.analyst import answer_with_data, generate_and_run_query
from app.agents.report import generate_report, no_evidence_report
from app.agents.research import answer_with_research
from app.database.errors import AnalyticsQueryFailedError
from app.database.repositories import ChunkRecord, DocumentRepository, new_id
from app.llm.schemas import AnalyticsQuery, SearchPlan
from app.tools.web_search import (
    WebResult,
    WebSearchFailedError,
    WebSearchNotConfiguredError,
)
from tests.fakes import FakeChatModel, FakeWebSearch, HashingEmbeddings

ALICE = "alice"

LEAVE_TEXT = (
    "Every full-time employee accrues twenty days of paid annual leave per calendar "
    "year, and unused leave carries over up to five days."
)

RESULT = WebResult(
    title="Leave entitlements in the UK",
    url="https://example.com/leave",
    content="Statutory minimum is 28 days including bank holidays.",
    score=0.88,
)


@pytest.fixture
def store(tmp_path: Path) -> DocumentRepository:
    return DocumentRepository(tmp_path / "agents" / "app.db")


@pytest.fixture
def embeddings() -> HashingEmbeddings:
    return HashingEmbeddings()


def seed_document(repository: DocumentRepository, embeddings: HashingEmbeddings) -> str:
    document = repository.create_document(
        owner_id=ALICE,
        filename="handbook.txt",
        content_type="text/plain",
        extension=".txt",
        size_bytes=len(LEAVE_TEXT),
    )
    repository.add_chunks(
        [
            ChunkRecord(
                id=new_id(),
                document_id=document.id,
                owner_id=ALICE,
                chunk_index=0,
                content=LEAVE_TEXT,
                page=1,
                section=None,
                embedding=np.array(embeddings.embed_query(LEAVE_TEXT), dtype=np.float32),
            )
        ]
    )
    repository.update_status(
        owner_id=ALICE, document_id=document.id, status="ready", chunk_count=1, mark_indexed=True
    )
    return document.id


def valid_query(sql: str = "select count(*) as n from demo_products") -> AnalyticsQuery:
    return AnalyticsQuery(sql=sql, reasoning="Answers the question directly.")


def prompt_text(model: FakeChatModel, index: int = -1) -> str:
    """The human message of a recorded call, which is where the data lives."""
    return str(model.received[index][-1].content)


def structured_prompt_text(model: FakeChatModel, index: int = -1) -> str:
    return str(model.structured_received[index][-1].content)


async def test_analyst_generates_runs_and_summarises(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["The demo catalogue contains 4 products."],
        structured_results=[valid_query()],
    )

    answer = await answer_with_data(
        store, model, question="How many products are in the catalogue?", max_rows=10
    )

    assert answer.grounded is True
    assert answer.analytics.rows == [{"n": 4}]
    assert answer.answer == "The demo catalogue contains 4 products."
    assert answer.usage is None
    assert '"n": 4' in prompt_text(model)
    assert "select count(*) as n from demo_products" in prompt_text(model)


async def test_analyst_repairs_a_rejected_query_once(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        structured_results=[
            valid_query("drop table demo_sales_orders"),
            valid_query(),
        ]
    )

    result = await generate_and_run_query(
        store, model, question="How many products?", max_rows=10
    )

    assert result.rows == [{"n": 4}]
    assert len(model.structured_received) == 2
    assert "rejected" not in structured_prompt_text(model, 0)
    assert "Only SELECT statements are allowed" in structured_prompt_text(model, 1)


async def test_analyst_repairs_a_database_error_with_its_message(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        structured_results=[
            valid_query("select revenu from demo_sales_orders"),
            valid_query("select count(*) as n from demo_sales_orders"),
        ]
    )

    result = await generate_and_run_query(
        store, model, question="Total revenue?", max_rows=10
    )

    assert result.rows == [{"n": 384}]
    assert "revenu" in structured_prompt_text(model, 1)


async def test_analyst_gives_up_after_one_repair(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        structured_results=[
            valid_query("select revenu from demo_sales_orders"),
            valid_query("select revenu from demo_sales_orders"),
        ]
    )

    with pytest.raises(AnalyticsQueryFailedError):
        await generate_and_run_query(store, model, question="Total revenue?", max_rows=10)

    assert len(model.structured_received) == 2


async def test_analyst_with_no_matching_rows_does_not_call_the_model(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["There are no products in the Missing category."],
        structured_results=[
            valid_query("select * from demo_products where category = 'Missing'")
        ],
    )

    answer = await answer_with_data(store, model, question="Missing category?", max_rows=10)

    assert answer.grounded is False
    assert answer.analytics.rows == []
    assert "returned no rows" in answer.answer
    assert model.received == []


async def test_research_dedupes_results_and_cites_them(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["Statutory leave is 28 days including bank holidays [W1]."],
        structured_results=[SearchPlan(queries=["statutory leave UK", "leave entitlement days"])],
    )
    search = FakeWebSearch(
        results=[RESULT, WebResult(title="Second", url="https://example.com/2", content="x")]
    )

    answer = await answer_with_research(
        search, model, question="How many days of leave are statutory?", max_results=5
    )

    assert answer.grounded is True
    assert [result.url for result in answer.results] == [RESULT.url, "https://example.com/2"]
    assert answer.queries == ["statutory leave UK", "leave entitlement days"]
    assert "[W1]" in prompt_text(model)
    assert RESULT.url in prompt_text(model)


async def test_research_bounds_the_merged_result_list(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["Answer."], structured_results=[SearchPlan(queries=["a", "b", "c"])]
    )
    search = FakeWebSearch(
        results=[
            WebResult(title=f"Result {index}", url=f"https://example.com/{index}", content="x")
            for index in range(10)
        ]
    )

    answer = await answer_with_research(search, model, question="Anything?", max_results=4)

    assert len(answer.results) == 4
    assert search.queries == ["a"]  # the cap is reached before the second query runs


async def test_research_with_no_results_is_honest_and_silent(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["This should never be used."],
        structured_results=[SearchPlan(queries=["an absurdly specific query"])],
    )
    search = FakeWebSearch(results=[])

    answer = await answer_with_research(search, model, question="Anything?", max_results=5)

    assert answer.grounded is False
    assert answer.results == []
    assert "no results" in answer.answer
    assert model.received == []


async def test_search_failure_is_raised_not_swallowed(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(structured_results=[SearchPlan(queries=["anything"])])
    search = FakeWebSearch(error=WebSearchFailedError())

    with pytest.raises(WebSearchFailedError):
        await answer_with_research(search, model, question="Anything?", max_results=5)


REPORT_KWARGS = {
    "top_k": 3,
    "max_context_chars": 4000,
    "min_score": 0.1,
    "max_rows": 10,
    "web_max_results": 5,
}


async def test_report_uses_the_corpus_and_carries_its_evidence(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    seed_document(store, embeddings)
    model = FakeChatModel(replies=["# Report\n\nEmployees accrue twenty days [1]."])

    outcome = await generate_report(
        store,
        embeddings,
        model,
        web_search=None,
        owner_id=ALICE,
        topic="How much annual leave do employees accrue?",
        sources=["documents"],
        **REPORT_KWARGS,
    )

    assert outcome.grounded is True
    assert outcome.report.startswith("# Report")
    assert [chunk.filename for chunk in outcome.evidence.documents] == ["handbook.txt"]
    assert outcome.evidence.searched_chunks == 1
    assert "[1] Source: handbook.txt" in prompt_text(model)


async def test_report_without_evidence_is_not_written_by_the_model(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(replies=["# Invented report\n\nEverything is fine."])

    outcome = await generate_report(
        store,
        embeddings,
        model,
        web_search=None,
        owner_id=ALICE,
        topic="Summarise the leave policy",
        sources=["documents"],
        **REPORT_KWARGS,
    )

    assert outcome.grounded is False
    assert "No evidence was found" in outcome.report
    assert outcome.report.startswith("# Report: Summarise the leave policy")
    assert outcome.evidence.documents == []
    assert model.received == []


async def test_report_web_without_a_provider_fails_loudly(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(replies=["# Report"])

    with pytest.raises(WebSearchNotConfiguredError):
        await generate_report(
            store,
            embeddings,
            model,
            web_search=None,
            owner_id=ALICE,
            topic="Latest UK leave law",
            sources=["web"],
            **REPORT_KWARGS,
        )


async def test_report_combines_documents_analytics_and_web(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    seed_document(store, embeddings)
    model = FakeChatModel(
        replies=["# Report\n\nTwenty days [1], four products, 28 days statutory [W1]."],
        structured_results=[
            valid_query("select count(*) as n from demo_products"),
            SearchPlan(queries=["statutory leave UK"]),
        ],
    )
    search = FakeWebSearch(results=[RESULT])

    outcome = await generate_report(
        store,
        embeddings,
        model,
        web_search=search,
        owner_id=ALICE,
        topic="Annual leave for employees",
        sources=["documents", "analytics", "web"],
        **REPORT_KWARGS,
    )

    assert outcome.grounded is True
    assert len(outcome.evidence.documents) == 1
    assert outcome.evidence.analytics is not None
    assert outcome.evidence.analytics.rows == [{"n": 4}]
    assert [result.url for result in outcome.evidence.web] == [RESULT.url]
    assert outcome.evidence.web_queries == ["statutory leave UK"]

    evidence_prompt = prompt_text(model)
    assert "<documents>" in evidence_prompt
    assert "<analytics>" in evidence_prompt
    assert "<web_results>" in evidence_prompt
    assert "Every requested source returned evidence" in evidence_prompt


async def test_partial_evidence_is_written_into_the_prompt_as_a_gap(
    store: DocumentRepository, embeddings: HashingEmbeddings
) -> None:
    model = FakeChatModel(
        replies=["# Report\n\nFour products, no internal documents were found."],
        structured_results=[valid_query("select count(*) as n from demo_products")],
    )

    outcome = await generate_report(
        store,
        embeddings,
        model,
        web_search=None,
        owner_id=ALICE,
        topic="Product catalogue",
        sources=["documents", "analytics"],
        **REPORT_KWARGS,
    )

    assert outcome.grounded is True
    assert "internal documents" in prompt_text(model)
    assert "These requested sources returned no evidence" in prompt_text(model)


def test_no_evidence_report_collapses_a_multi_line_topic() -> None:
    report = no_evidence_report(topic="Leave policy\n\n# Injected heading", gaps=["web search"])

    assert "\n# Injected heading" not in report
    assert "# Report: Leave policy # Injected heading" in report
    assert "web search" in report
