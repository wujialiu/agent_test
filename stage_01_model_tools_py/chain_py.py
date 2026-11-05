"""LCEL composition using the same tested assistant and tool boundary."""

from .agent_py import Assistant, Session


def conversation_chain(assistant: Assistant, session: Session):
    from langchain_core.runnables import RunnableLambda

    async def answer(prompt: str):
        return await assistant.ask(prompt, session)

    # Compare this input → agent composition with calling ask() directly.
    # The pipe wires them together without changing validation or permissions.
    return RunnableLambda(str.strip) | RunnableLambda(answer)
