"""State passed between the nodes of the agent graph."""

import operator
from typing import Annotated

from pydantic import BaseModel, Field

from app.messages import IncomingEmail


class StepResult(BaseModel):
    agent: str
    output: str


class RoutingDecision(BaseModel):
    agents: list[str] = Field(description="Specialists to run, in execution order. Empty if unclear.")
    confidence: float = Field(ge=0, le=1, description="How sure you are the plan covers the request")
    reasoning: str = Field(description="One sentence on why these specialists")
    clarification: str = Field(default="", description="Question to the user when the request is unclear")


class AgentState(BaseModel):
    email: IncomingEmail
    model: str | None = None
    plan: list[str] = Field(default_factory=list)
    results: Annotated[list[StepResult], operator.add] = Field(default_factory=list)
    response: str = ""

    @property
    def next_agent(self) -> str | None:
        step = len(self.results)
        return self.plan[step] if step < len(self.plan) else None
