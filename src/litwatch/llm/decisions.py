"""A decision request as a chat with a strict schema, and the answer both ways share.

Every decision is validated as the chat answer would be: a Decisions endpoint's reply is
translated into the same JSON (`openrouter.decision_reply`), so the journal, reuse and
validation are the chat path's.
"""

from __future__ import annotations

from functools import cache
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from litwatch.llm.types import Choice, DecisionRequest, Predicate, Question, Request

type Probability = Annotated[float, Field(ge=0, le=1)]


class Chosen(BaseModel):
    option: str
    probability: float | None  # a Decisions endpoint may leave it out


class Decision(BaseModel):
    """Per yes/no question the probability of yes; per choice the option and its probability."""

    probabilities: dict[str, float]
    choices: dict[str, Chosen]


@cache
def output_model(questions: tuple[Question, ...]) -> type[BaseModel]:
    """The answer's schema: one property per question, named as the question."""
    fields: dict[str, Any] = {}
    for i, q in enumerate(questions):
        match q:
            case Predicate():
                kind: Any = Probability
            case Choice():
                labels = tuple(label for label, _ in q.options)
                kind = create_model(
                    f"Choice{i}",
                    option=(Literal[labels], ...),  # type: ignore[valid-type]
                    probability=(Probability | None, ...),
                )
        fields[f"q{i}"] = (kind, Field(alias=q.name))
    # The journal keeps a validated answer by field name, and reuse reads it back that way.
    config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)
    return create_model("Decision", __config__=config, **fields)


def chat_request(request: DecisionRequest) -> Request:
    """The questions in the system prompt, which repeats across papers and is cached; the
    content as the user message."""
    lines = []
    for q in request.questions:
        match q:
            case Predicate():
                lines.append(f"- `{q.name}` (yes/no): {q.instructions}")
            case Choice():
                lines.append(f"- `{q.name}` (choice): {q.instructions} The options:")
                lines += [f"  - `{label}`: {meaning}" for label, meaning in q.options]
    system = (
        "Answer every question below about the content you are given.\n"
        "- A yes/no question: the probability, from 0 to 1, that the answer is yes.\n"
        "- A choice: the one option that fits best, written exactly as listed, and the "
        "probability, from 0 to 1, that it is the right one.\n\n"
        "The questions:\n" + "\n".join(lines)
    )
    return Request(system=system, user=request.content, output=output_model(request.questions))


def decision_of(value: BaseModel, request: DecisionRequest) -> Decision:
    answers = value.model_dump(by_alias=True)
    probabilities, choices = {}, {}
    for q in request.questions:
        if isinstance(q, Predicate):
            probabilities[q.name] = answers[q.name]
        else:
            choices[q.name] = Chosen.model_validate(answers[q.name])
    return Decision(probabilities=probabilities, choices=choices)
