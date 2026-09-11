"""Router evaluation: does the LLM pick the right specialist chain for an email?

30 labelled emails (single specialists, two-step chains, attachments, and vague
requests that should get a clarifying question instead of a guess). A case can
accept several plans when more than one is reasonable.

    python -m evals.routing                                   # DEFAULT_MODEL
    python -m evals.routing --models gemini-2.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite --repeat 3
    python -m evals.routing --offline                         # plumbing check with a stub router

Reported per model: exact-plan accuracy, first-step accuracy, accuracy on
clarification cases, per-specialist precision/recall, run-to-run consistency,
router latency p50/p95, tokens and cost per routing decision.
"""

import argparse
import asyncio
import json
from collections import defaultdict

from app import telemetry
from app.agents import graph
from app.agents.state import AgentState, RoutingDecision
from app.config import SqlDatabase, settings
from app.messages import Attachment, IncomingEmail
from demo.seed_crm import DESCRIPTION
from evals import common
from evals.metrics import mean, percentile, plan_matches

DEMO_DATABASES = {"crm": SqlDatabase(url="sqlite+aiosqlite:///:memory:", description=DESCRIPTION)}


def email_for(case: dict) -> IncomingEmail:
    return IncomingEmail(
        id=case["id"],
        thread_id=case["id"],
        sender="eval@example.com",
        subject=case["subject"],
        body=case["body"],
        attachments=[
            Attachment(filename=name, mime_type="application/octet-stream", data=b"")
            for name in case.get("attachments", [])
        ],
    )


def closest_accepted(plan: list[str], accepted: list[list[str]]) -> list[str]:
    return max(accepted, key=lambda option: len(set(option) & set(plan)) - abs(len(option) - len(plan)))


async def route_case(case: dict, model: str | None) -> dict:
    error, decision, plan = None, None, []
    with telemetry.trace() as trace:
        try:
            decision, plan = await graph.plan_request(AgentState(email=email_for(case), model=model))
        except Exception as exc:  # counted as a wrong decision, the run continues
            error = str(exc).splitlines()[0][:200]
    summary = trace.summary()
    return {
        "plan": plan,
        "error": error,
        "confidence": decision.confidence if decision else 0.0,
        "reasoning": decision.reasoning if decision else "",
        "latency_s": summary["spans"].get("router", summary["total_s"]),
        "cost_usd": summary["cost_usd"],
        "tokens": sum(t["in"] + t["out"] for t in summary["tokens"].values()),
    }


def evaluate(cases: list[dict], runs: list[dict[str, dict]]) -> dict:
    """`runs` = one {case_id: result} mapping per repetition."""
    exact, first, clarify = [], [], []
    per_agent = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for run in runs:
        for case in cases:
            result, accepted = run[case["id"]], case["accept"]
            plan, failed = result["plan"], bool(result.get("error"))
            exact.append(float(not failed and plan_matches(plan, accepted)))
            if accepted == [[]]:
                clarify.append(float(not failed and plan == []))
                continue
            first.append(float(bool(plan) and any(option and plan[0] == option[0] for option in accepted)))
            target = set(closest_accepted(plan, accepted))
            for agent in set(plan) | target:
                key = "tp" if agent in plan and agent in target else ("fp" if agent in plan else "fn")
                per_agent[agent][key] += 1

    consistent = (
        [float(len({tuple(run[case["id"]]["plan"]) for run in runs}) == 1) for case in cases] if len(runs) > 1 else []
    )
    results = [r for run in runs for r in run.values()]
    costs = [r["cost_usd"] for r in results if r["cost_usd"] is not None]
    return {
        "errors": sum(bool(r.get("error")) for r in results),
        "exact_plan_accuracy": mean(exact),
        "first_step_accuracy": mean(first),
        "clarification_accuracy": mean(clarify),
        "consistency": mean(consistent),
        "latency_p50_s": percentile([r["latency_s"] for r in results], 50),
        "latency_p95_s": percentile([r["latency_s"] for r in results], 95),
        "tokens_mean": mean([r["tokens"] for r in results]),
        "cost_per_routing_usd": mean(costs) if costs else float("nan"),
        "per_agent": {
            agent: {
                "precision": c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else float("nan"),
                "recall": c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else float("nan"),
            }
            for agent, c in sorted(per_agent.items())
        },
    }


def to_markdown(meta: dict, by_model: dict[str, dict], errors: dict[str, list[str]]) -> str:
    f = common.fmt
    out = ["# Routing evaluation", "", f"Run {meta['run_at']} · {meta['cases']} cases × {meta['repeat']} run(s)", ""]
    if meta["offline"]:
        out += ["> **Offline plumbing check** — stub router, numbers are not meaningful.", ""]
    out += [
        "| Model | Exact plan | First step | Clarifies vague | Consistency "
        "| Latency p50 / p95 | Tokens | Cost / routing | Errors |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for model, m in by_model.items():
        cost = "–" if m["cost_per_routing_usd"] != m["cost_per_routing_usd"] else f"${m['cost_per_routing_usd']:.5f}"
        out.append(
            f"| `{model}` | {f(m['exact_plan_accuracy'])} | {f(m['first_step_accuracy'])} | "
            f"{f(m['clarification_accuracy'])} | {f(m['consistency'])} | "
            f"{f(m['latency_p50_s'])} s / {f(m['latency_p95_s'])} s | {f(m['tokens_mean'], 0)} | {cost} "
            f"| {m['errors']} |"
        )
    for model, m in by_model.items():
        out += ["", f"**Per specialist — `{model}`:**", "", "| Specialist | Precision | Recall |", "|---|---|---|"]
        out += [f"| {a} | {f(v['precision'])} | {f(v['recall'])} |" for a, v in m["per_agent"].items()]
        out += ["", "Misrouted:", "", *(errors[model] or ["- none"])]
    return "\n".join(out) + "\n"


def misrouted(case: dict, result: dict) -> str:
    expected = " or ".join(map(str, case["accept"]))
    got = f"error: {result['error']}" if result.get("error") else result["plan"]
    return f"- `{case['id']}` {case['body'][:70]!r} → {got} (expected {expected})"


def use_stub_router() -> None:
    async def stub_run_chat(model, fn, **limits):
        return RoutingDecision(agents=["assistant"], confidence=0.9, reasoning="stub")

    graph.run_chat = stub_run_chat


async def main(models: list[str], repeat: int, offline: bool) -> None:
    settings.sql_databases = DEMO_DATABASES
    settings.finance_enabled = True
    if offline:
        use_stub_router()
    cases = common.load_jsonl(common.EVALS_DIR / "routing_cases.jsonl")

    by_model, errors, raw, failed = {}, {}, {}, {}
    for model in models:
        runs = []
        settings.router_model = model  # the router uses ROUTER_MODEL when it is set
        try:
            for attempt in range(repeat):
                print(f"[{model}] run {attempt + 1}/{repeat}")
                runs.append({case["id"]: await route_case(case, model) for case in cases})
        except Exception as exc:  # e.g. a model id that is not available to this API key
            print(f"[{model}] failed: {exc}")
            failed[model] = str(exc).splitlines()[0][:300]
            continue
        by_model[model] = evaluate(cases, runs)
        errors[model] = [
            misrouted(c, runs[0][c["id"]])
            for c in cases
            if runs[0][c["id"]].get("error") or not plan_matches(runs[0][c["id"]]["plan"], c["accept"])
        ]
        raw[model] = runs

    meta = common.run_metadata(offline, cases=len(cases), repeat=repeat, models=models, failed_models=failed)
    report = to_markdown(meta, by_model, errors)
    if failed:
        report += (
            "\n**Models that could not be evaluated:**\n\n"
            + "\n".join(f"- `{m}`: {reason}" for m, reason in failed.items())
            + "\n"
        )
    print("\n" + report)
    common.results_path("routing", offline, "md").write_text(report, encoding="utf-8")
    common.results_path("routing", offline, "json").write_text(
        json.dumps({"meta": meta, "models": by_model, "runs": raw}, indent=2, default=str), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=settings.default_model, help="Comma-separated model names")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per model (measures consistency)")
    parser.add_argument("--offline", action="store_true", help="Stub router, no API key needed")
    args = parser.parse_args()
    asyncio.run(main([m.strip() for m in args.models.split(",") if m.strip()], args.repeat, args.offline))
