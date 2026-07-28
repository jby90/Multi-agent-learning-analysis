"""Production dependency wiring for live REF agents."""

from __future__ import annotations

from pathlib import Path

from agents.diagnosis_agent import DiagnosisAgent
from agents.kb_loader import require_valid_chunks
from agents.knowledge_agent import KnowledgeAgent
from agents.rebuttal_generator import RebuttalGenerator
from agents.retriever import BM25Retriever
from agents.review_agent import ReviewAgent
from agents.sandbox import DatabaseSettings, ReadOnlyExecutor
from agents.task_agent import TaskAgent
from agents.verification_agent import VerificationAgent
from coordination.parallel import bounded_llm_executor
from orchestrator.llm import call_llm, warm_default_client


def build_verification_agent(trace_id: str) -> VerificationAgent:
    """Build the live verification agent without altering test stub wiring."""

    warm_default_client()
    return VerificationAgent(
        trace_id=trace_id,
        llm_call=call_llm,
        executor=ReadOnlyExecutor(DatabaseSettings.from_environment()),
    )


def build_knowledge_agent(trace_id: str) -> KnowledgeAgent:
    """Build the live knowledge agent and reject any partial knowledge base."""

    chunk_directory = (
        Path(__file__).resolve().parents[1]
        / "agents"
        / "knowledge_base"
        / "chunks"
    )
    chunks = require_valid_chunks(chunk_directory)
    return KnowledgeAgent(
        trace_id=trace_id,
        retriever=BM25Retriever(chunks),
        llm_call=call_llm,
    )


def build_diagnosis_agent(trace_id: str) -> DiagnosisAgent:
    """Build the live diagnosis agent with its guarded 32B narrative layer."""

    warm_default_client()
    return DiagnosisAgent(trace_id=trace_id, llm_call=call_llm)


def build_task_agent(trace_id: str) -> TaskAgent:
    """Build the live task agent with its guarded 235B display layer."""

    warm_default_client()
    return TaskAgent(trace_id=trace_id, llm_call=call_llm)


def build_review_agent(trace_id: str) -> ReviewAgent:
    """Build the live 32B review agent after warming the shared client."""

    warm_default_client()
    return ReviewAgent(
        trace_id=trace_id,
        llm_call=call_llm,
        parallel_executor=bounded_llm_executor(call_llm, max_concurrency=2),
    )


def build_rebuttal_generator(trace_id: str) -> RebuttalGenerator:
    """Build the live 235B rebuttal generator after warming the shared client."""

    warm_default_client()
    return RebuttalGenerator(trace_id=trace_id, llm_call=call_llm)
