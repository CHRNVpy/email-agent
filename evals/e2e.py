"""End-to-end latency and cost: full requests through router -> specialists -> reply.

Uses the demo CRM (seeded automatically) and the eval corpus as the knowledge
base. Needs a Gemini key and Qdrant; no Gmail or Google OAuth.

    python -m evals.e2e                 # every request once
    python -m evals.e2e --repeat 3      # better p95
    python -m evals.e2e --only sql-top-customers,kb-refund

Cost = tokens x the Gemini paid-tier price table in app/telemetry.py. Not included:
Google Search grounding ($35 / 1,000 grounded prompts after the free daily quota)
and Alpha Vantage. Embedding tokens are estimated from text length.
"""

import argparse
import asyncio
import json
from pathlib import Path

from app import telemetry
from app.agents import graph
from app.config import SqlDatabase, settings
from app.messages import IncomingEmail
from app.permissions.policy import Principal
from app.permissions.service import current_principal, set_principal
from app.pipeline import answer
from app.rag import store
from app.rag.indexers import index_local_files
from demo.seed_crm import DEFAULT_PATH, DESCRIPTION, NOTES, seed
from evals import common
from evals.metrics import mean, percentile

REQUESTS = [
    (
        "sql-top-customers",
        "Top customers",
        "Who are our top 5 customers by revenue this quarter? Include revenue and account manager.",
    ),
    (
        "sql-overdue",
        "Overdue invoices",
        "List invoices that are more than 30 days overdue with customer name, amount and days overdue.",
    ),
    ("kb-refund", "Refunds", "What is our refund policy for annual plans?"),
    ("kb-june", "June decisions", "What did we decide in our meetings in June 2026?"),
    ("kb-unanswerable", "Parental leave", "What is our parental leave policy?"),
    ("finance-news", "NVIDIA", "Summarise the latest news about NVIDIA stock."),
    ("web-ai-act", "EU AI Act", "What are the upcoming enforcement dates of the EU AI Act?"),
    ("assistant-hello", "Hello", "Hi! What can you help me with?"),
    (
        "chain-reminder",
        "Payment reminder",
        "Find the customer with the largest overdue invoice and draft a polite payment reminder email to them.",
    ),
]
COLLECTION = "eval_e2e"


async def prepare(reindex: bool) -> None:
    crm = DEFAULT_PATH if DEFAULT_PATH.exists() else seed(DEFAULT_PATH)
    settings.sql_databases = {
        "crm": SqlDatabase(
            url=f"sqlite+aiosqlite:///{Path(crm).resolve()}",
            description=DESCRIPTION,
            notes=NOTES,
        )
    }
    settings.workflow_sheet_id = ""  # measure the agent graph only
    settings.qdrant_collection = COLLECTION
    if reindex or not await store.qdrant().collection_exists(COLLECTION):
        if await store.qdrant().collection_exists(COLLECTION):
            await store.qdrant().delete_collection(COLLECTION)
        await index_local_files(common.CORPUS_DIR)
    graph.build_graph.cache_clear()
    set_principal(Principal("eval@example.com", unrestricted=True))


async def run_request(request_id: str, subject: str, body: str, model: str | None) -> dict:
    email = IncomingEmail(id=request_id, thread_id=request_id, sender="eval@example.com", subject=subject, body=body)
    with telemetry.trace() as trace:
        try:
            reply, error = await answer(email, current_principal(), model), None
        except Exception as exc:  # keep measuring the other requests
            reply, error = "", repr(exc)
    summary = trace.summary()
    return {
        "id": request_id,
        "plan": [name.removeprefix("agent.") for name in summary["spans"] if name.startswith("agent.")],
        "total_s": summary["total_s"],
        "spans": summary["spans"],
        "tokens": sum(t["in"] + t["out"] for t in summary["tokens"].values()),
        "cost_usd": summary["cost_usd"],
        "tools": summary["tools"],
        "reply": reply,
        "error": error,
    }


def aggregate(results: list[dict]) -> dict:
    ok = [r for r in results if not r["error"]]
    costs = [r["cost_usd"] for r in ok if r["cost_usd"] is not None]
    return {
        "requests": len(results),
        "errors": len(results) - len(ok),
        "latency_p50_s": percentile([r["total_s"] for r in ok], 50),
        "latency_p95_s": percentile([r["total_s"] for r in ok], 95),
        "cost_mean_usd": mean(costs) if costs else float("nan"),
        "cost_max_usd": max(costs) if costs else float("nan"),
        "tokens_mean": mean([r["tokens"] for r in ok]),
    }


def to_markdown(meta: dict, results: list[dict], totals: dict) -> str:
    f = common.fmt
    by_id: dict[str, list[dict]] = {}
    for r in results:
        by_id.setdefault(r["id"], []).append(r)
    out = [
        "# End-to-end latency & cost",
        "",
        f"Run {meta['run_at']} · model `{meta['default_model']}` · {totals['requests']} requests "
        f"({meta['repeat']} run(s) of {len(by_id)}) · errors {totals['errors']}",
        "",
        f"**Overall:** latency p50 {f(totals['latency_p50_s'], 1)} s, p95 {f(totals['latency_p95_s'], 1)} s · "
        f"cost per request mean ${totals['cost_mean_usd']:.4f}, max ${totals['cost_max_usd']:.4f} · "
        f"{f(totals['tokens_mean'], 0)} tokens on average",
        "",
        "| Request | Plan | Latency (median) | Router | Specialists | Tokens | Cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for request_id, runs in by_id.items():
        ok = [r for r in runs if not r["error"]] or runs
        median = sorted(ok, key=lambda r: r["total_s"])[len(ok) // 2]
        specialists = sum(v for k, v in median["spans"].items() if k.startswith("agent."))
        cost = "–" if median["cost_usd"] is None else f"${median['cost_usd']:.4f}"
        plan = " → ".join(median["plan"]) or ("error" if median["error"] else "clarification")
        out.append(
            f"| {request_id} | {plan} | {f(median['total_s'], 1)} s | {f(median['spans'].get('router', 0), 1)} s | "
            f"{f(specialists, 1)} s | {median['tokens']} | {cost} |"
        )
    unanswerable = next((r for r in results if r["id"] == "kb-unanswerable" and not r["error"]), None)
    if unanswerable:
        excerpt = " ".join(unanswerable["reply"].split())[:300]
        out += ["", f"**Abstention check** — reply to a question the knowledge base cannot answer: “{excerpt}…”"]
    out += [
        "",
        "Cost uses the Gemini paid-tier price table in `app/telemetry.py`; Google Search grounding fees "
        "and Alpha Vantage are not included, embedding tokens are estimated.",
    ]
    return "\n".join(out) + "\n"


async def main(repeat: int, only: set[str], model: str | None, reindex: bool) -> None:
    await prepare(reindex)
    results = []
    for attempt in range(repeat):
        for request_id, subject, body in REQUESTS:
            if only and request_id not in only:
                continue
            print(f"[run {attempt + 1}/{repeat}] {request_id}")
            results.append(await run_request(request_id, subject, body, model))
    totals = aggregate(results)
    meta = common.run_metadata(False, repeat=repeat)
    if model:
        meta["default_model"] = model
    report = to_markdown(meta, results, totals)
    print("\n" + report)
    common.results_path("e2e", False, "md").write_text(report, encoding="utf-8")
    common.results_path("e2e", False, "json").write_text(
        json.dumps({"meta": meta, "totals": totals, "results": results}, indent=2, default=str), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", default="", help="Comma-separated request ids")
    parser.add_argument("--model", help="Override DEFAULT_MODEL")
    parser.add_argument("--reindex", action="store_true", help="Rebuild the eval knowledge-base collection")
    args = parser.parse_args()
    asyncio.run(main(args.repeat, {x for x in args.only.split(",") if x}, args.model, args.reindex))
