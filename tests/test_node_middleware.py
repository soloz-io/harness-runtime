"""An acrylic node gets the stack create_deep_agent gives a subagent, summarization included."""

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from core.graph.node_compiler import build_node_middleware


def _names(stack):
    return [type(m).__name__ for m in stack]


def test_a_node_compacts_its_history_instead_of_overflowing():
    names = _names(build_node_middleware({}, FakeListChatModel(responses=["ok"])))
    summarization = next(n for n in names if "Summarization" in n)
    # deepagents' order: filesystem, then summarization, then tool-call patching.
    assert (
        names.index("FilesystemMiddleware")
        < names.index(summarization)
        < names.index("PatchToolCallsMiddleware")
    )


def test_interrupts_and_structured_output_still_follow():
    names = _names(
        build_node_middleware(
            {"interrupt_on": {"write_file": True}},
            FakeListChatModel(responses=["ok"]),
            {"type": "object"},
        )
    )
    assert names[-2:] == ["HumanInTheLoopMiddleware", "StructuredOutputMappingMiddleware"]
