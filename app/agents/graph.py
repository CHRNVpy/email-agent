"""The orchestration graph: router -> specialist(s) -> reply.

router ──► specialist A ──► specialist B ──► finish
   │            (each step sees the previous results)
   └──► finish (clarifying question)
"""

import logging
from functools import cache

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph

from app.agents import prompts
from app.agents.content import request_text
from app.agents.specialists import Specialist, available_specialists, describe
from app.agents.state import AgentState, RoutingDecision, StepResult
from app.config import settings
from app.llm import run_chat
from app.messages import IncomingEmail
from app.permissions.service import current_principal

logger = logging.getLogger(__name__)

FINISH = "finish"
FALLBACK_REPLY = "I'm not sure how to help with this request. Could you describe what you need in more detail?"

_ROUTER_PROMPT = ChatPromptTemplate.from_messages([("system", prompts.ROUTER), ("human", "{request}")])


async def route(state: AgentState) -> dict:
    specialists = available_specialists()

    async def call(llm):
        chain = _ROUTER_PROMPT | llm.with_structured_output(RoutingDecision)
        return await chain.ainvoke({"specialists": describe(specialists), "request": request_text(state)})

    decision: RoutingDecision = await run_chat(state.model, call)
    known = {s.name for s in specialists}
    plan = [name for name in decision.agents if name in known]
    logger.info("Route %s (confidence %.2f): %s", plan, decision.confidence, decision.reasoning)

    if not plan or decision.confidence < settings.router_confidence_threshold:
        return {"plan": [], "response": decision.clarification or FALLBACK_REPLY}
    return {"plan": plan}


def make_node(specialist: Specialist):
    async def node(state: AgentState) -> dict:
        if not specialist.is_allowed(current_principal()):
            output = f"(You don't have permission to use the {specialist.name} capability.)"
        else:
            try:
                output = await specialist.run(state)
            except Exception:
                logger.exception("Specialist %s failed", specialist.name)
                output = f"(The {specialist.name} step failed due to an internal error.)"
        return {"results": [StepResult(agent=specialist.name, output=output)]}

    node.__name__ = specialist.name
    return node


def next_step(state: AgentState) -> str:
    return state.next_agent or FINISH


def finish(state: AgentState) -> dict:
    if state.response:
        return {}
    # The last specialist worked with all earlier results, so its output is the reply.
    return {"response": state.results[-1].output if state.results else FALLBACK_REPLY}


@cache
def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("router", route)
    graph.add_node(FINISH, finish)

    specialists = available_specialists()
    targets = {s.name: s.name for s in specialists} | {FINISH: FINISH}
    for specialist in specialists:
        graph.add_node(specialist.name, make_node(specialist))
        graph.add_conditional_edges(specialist.name, next_step, targets)

    graph.add_edge(START, "router")
    graph.add_conditional_edges("router", next_step, targets)
    graph.add_edge(FINISH, END)
    return graph.compile()


async def run_agent(email: IncomingEmail, model: str | None = None) -> str:
    final = await build_graph().ainvoke(AgentState(email=email, model=model))
    return final["response"]
