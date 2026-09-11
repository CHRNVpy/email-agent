import pytest

from app.workflows import engine, store
from app.workflows.models import ResolvedSource, RunStatus
from app.workflows.sheet import parse_row


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace stage execution with a recorder that echoes the prompt."""
    calls: list[str] = []

    async def execute_stage(run, prompt, scope):
        calls.append(prompt)
        return f"answer #{len(calls)}"

    async def resolve_all(sources):
        return {
            name: ResolvedSource(content=f"<{name}>", items=["AAA", "BBB"] if s.iterate else [])
            for name, s in sources.items()
        }

    monkeypatch.setattr(engine, "execute_stage", execute_stage)
    monkeypatch.setattr(engine, "resolve_all", resolve_all)
    return calls


def workflow(**stages):
    return parse_row({"workflow": "Report", "source_data": "facts", **stages})


async def test_stages_run_in_order_with_sources_substituted(fake_llm):
    run = await engine.start_workflow(
        workflow(stage_1="Analyse {source_data}", stage_2="Summarise"),
        user_email="a@example.com",
        thread_id="t1",
        request="Please run the report",
    )
    assert run.status == RunStatus.COMPLETED
    assert fake_llm == ["Analyse <source_data>", "Summarise"]
    assert run.completed_stages == 2
    assert engine.final_message(run) == "answer #2"
    assert run.history[0]["content"].endswith("Please run the report")


async def test_await_reply_pauses_and_resumes(fake_llm):
    config = workflow(stage_1="Draft an outline", stage_2="Is this outline OK? {await_reply}", stage_3="Write it")
    run = await engine.start_workflow(config, user_email="a@example.com", thread_id="t1", request="")

    assert run.status == RunStatus.AWAITING_REPLY
    assert run.completed_stages == 1
    assert engine.final_message(run) == "Is this outline OK?"

    run = await engine.resume_workflow(run, "Yes, add a risks section")
    assert run.status == RunStatus.COMPLETED
    assert fake_llm[1] == "Is this outline OK?\n\nThe user replied:\nYes, add a risks section"
    assert fake_llm[2] == "Write it"


async def test_iteration_runs_stage_per_item(fake_llm):
    config = parse_row(
        {"workflow": "Per ticker", "source_tickers": "iteration=TRUE; AAA\nBBB", "stage_1": "Research {source_tickers}"}
    )
    run = await engine.start_workflow(config, user_email="a@example.com", thread_id="t", request="")
    assert fake_llm == ["Research AAA", "Research BBB"]
    assert "### Item 2\nanswer #2" in run.last_answer


async def test_failure_marks_run_failed(monkeypatch, fake_llm):
    async def boom(run, prompt, scope):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(engine, "execute_stage", boom)
    run = await engine.start_workflow(workflow(stage_1="x"), user_email="a@example.com", thread_id="t", request="")
    assert run.status == RunStatus.FAILED
    assert "model unavailable" in engine.final_message(run)


async def test_paused_run_roundtrips_through_store(app_db, fake_llm):
    config = workflow(stage_1="Question? {await_reply}", stage_2="Done")
    run = await engine.start_workflow(config, user_email="a@example.com", thread_id="thread-9", request="")
    await store.save(run)

    loaded = await store.find_awaiting_reply("thread-9")
    assert loaded is not None and loaded.id == run.id
    assert loaded.config.stages[0].awaits_reply

    loaded = await engine.resume_workflow(loaded, "ok")
    await store.save(loaded)
    assert await store.find_awaiting_reply("thread-9") is None
