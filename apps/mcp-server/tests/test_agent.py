"""sharepoint_ask's LangGraph loop with a scripted fake chat model (no API calls)."""
import asyncio
import json
import time

import langsmith as ls
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langsmith import Client
from sharepoint_rag.search import Hit

from mcp_server import agent


def call(name, args, id="t1"):
    return {"name": name, "args": args, "id": id, "type": "tool_call"}


def reply(*calls, stop="tool_use", text=""):
    return AIMessage(
        content=text, tool_calls=list(calls), response_metadata={"stop_reason": stop},
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    )


def final(*citations, found="yes", answer="22 days."):
    return reply(call("final_answer", {"found": found, "answer": answer, "citations": list(citations)}))


class FakeModel:
    """Stands in for any LangChain chat model: bind_tools + ainvoke."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[list] = []
        self.bound_tools = None
        self.tool_choices: list[str | None] = []  # per request: the tool the model had to call, if any

    def bind_tools(self, tools, tool_choice=None):
        self.bound_tools = tools
        return Bound(self, tool_choice)

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Bound:
    def __init__(self, model, tool_choice):
        self.model, self.tool_choice = model, tool_choice

    async def ainvoke(self, messages):
        self.model.tool_choices.append(self.tool_choice)
        return await self.model.ainvoke(messages)


def hit(ordinal, text, heading="leave-policy > Page 2", **kw):
    return Hit(**{"chunk_id": ordinal, "source_id": 7, "name": "leave-policy.pdf", "web_url": "https://sp/leave",
                  "heading_path": heading, "text": text, "drive_id": "d", "item_id": "i", "score": 1.0,
                  "ordinal": ordinal, **kw})


HIT = hit(5, "Employees get 22 working days of annual leave.")
# The file around HIT: the table's heading before it, its footnote after.
AROUND = [hit(4, "Table 3. Annual leave, full-time staff only.", "leave-policy > Page 1"), HIT,
          hit(6, "Part-time staff get leave pro rata.", "leave-policy > Page 3")]


def tools():
    async def search(q, k):
        return [HIT]

    async def read_file(d, i):
        return {"name": "leave-policy.md", "text": "Full text: 22 working days. Carry over 5.", "webUrl": "https://sp/leave", "parentId": "folder1"}

    async def list_items(d, i):
        return [{"id": "x", "name": "other.md", "webUrl": "https://sp/other", "file": {}}]

    async def nearby(h, before, after):
        return [a for a in AROUND if h.ordinal - before <= a.ordinal <= h.ordinal + after]

    return agent.AskTools(search=search, read_file=read_file, list_items=list_items, nearby=nearby)


async def run(model, ask_tools=None, **kw):
    args = dict(max_steps=6, max_tokens=100_000, timeout_seconds=30)
    default = sandbox_tools() if kw.get("sandbox") else tools()
    return await agent.ask(model, "How many leave days?", ask_tools or default, **{**args, **kw})


async def test_answer_with_validated_sources():
    model = FakeModel([
        reply(call("search", {"query": "annual leave days", "limit": 5})),
        final({"source_id": "S1", "quote": "22 working days of annual leave"},
              answer="22 days. See https://sp/leave or https://evil.example/x"),
    ])
    result = await run(model)
    assert result.status == "answered" and result.steps == 2 and result.tokens == 300
    assert result.sources == [{"title": "leave-policy.pdf", "link": "https://sp/leave", "quote": "22 working days of annual leave",
                               "sections": ["leave-policy > Page 2"]}]
    assert "https://sp/leave" in result.answer and "evil.example" not in result.answer
    assert [t["name"] for t in model.bound_tools] == ["search", "read_around", "read_file", "list_items", "final_answer"]
    last = model.requests[1][-1]
    assert isinstance(last, ToolMessage) and last.tool_call_id == "t1"
    assert '"passage": "P1"' in last.content and '"source_id": "S1"' in last.content


async def test_the_users_region_goes_with_the_question_not_the_system_prompt():
    plain, placed = (FakeModel([final({"source_id": "S1", "quote": "x"})]) for _ in range(2))
    await run(plain)
    await run(placed, region="the United States")
    assert plain.requests[0][1].content == "How many leave days?"
    assert placed.requests[0][1].content == "The user is based in the United States.\n\nHow many leave days?"
    assert placed.requests[0][0].content == plain.requests[0][0].content


@pytest.mark.parametrize("prompt", [agent.SYSTEM, agent.SYSTEM_SANDBOX])
def test_the_prompt_asks_for_the_model_instead_of_guessing_between_vehicles(prompt):
    assert "Please specify the make and model" in prompt and "can't be relied on" in prompt


async def test_rejected_citations_get_one_chance_to_be_fixed():
    """A quote that isn't in the source is sent back once, so the fact's source isn't silently lost."""
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        final({"source_id": "S1", "quote": "22 working days of annual leave"},
              {"source_id": "S1", "quote": "a quote that is not in the document"},
              {"source_id": "S99", "quote": "never retrieved"}),
        final({"source_id": "S1", "quote": "Employees get 22 working days"}, {"source_id": "S99", "quote": "still wrong"}),
    ])
    result = await run(model)
    feedback = model.requests[2][-1]
    assert isinstance(feedback, ToolMessage) and feedback.status == "error"
    assert "not found word for word" in feedback.content and "S99: no such source" in feedback.content
    assert "22 working days of annual leave" not in feedback.content  # the good citation isn't questioned
    # The second answer is accepted as it validates; there is no second round.
    assert result.status == "answered" and result.steps == 3
    assert result.sources[0]["quote"] == "Employees get 22 working days"


async def test_a_plain_text_reply_is_told_once_to_answer_through_final_answer():
    """Ending without final_answer would return an answer with no sources, so the model is sent back once."""
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        AIMessage(content="22 days of leave."),
        final({"source_id": "S1", "quote": "22 working days of annual leave"}, answer="22 days."),
    ])
    result = await run(model)
    assert isinstance(model.requests[2][-1], HumanMessage) and "final_answer" in model.requests[2][-1].content
    assert result.status == "answered" and result.steps == 3 and result.sources[0]["quote"] == "22 working days of annual leave"


async def test_a_second_plain_text_reply_is_kept_without_sources():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        AIMessage(content="22 days."),
        AIMessage(content="22 days of leave."),
    ])
    result = await run(model)
    assert result.status == "answered" and result.answer == "22 days of leave." and result.sources == [] and result.steps == 3


async def test_no_plain_text_nudge_without_room_left():
    model = FakeModel([reply(call("search", {"query": "leave", "limit": 5})), AIMessage(content="22 days.")])
    result = await run(model, max_steps=2)
    assert result.status == "answered" and result.sources == [] and result.steps == 2


async def test_no_fix_round_without_room_left():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        final({"source_id": "S1", "quote": "22 working days"}, {"source_id": "S1", "quote": "not in the document"}),
    ])
    result = await run(model, max_steps=2)
    assert result.status == "answered" and result.steps == 2
    assert result.sources[0]["quote"] == "22 working days"


async def test_when_no_citation_checks_out_on_the_last_step_the_fix_may_take_one_step_more():
    """A table's columns interleaved in a PDF's text: every quote is rejected, and without the fix round the right
    answer would go back with no source at all."""
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        final({"source_id": "S1", "quote": "22 working days of annual leave, part-time pro rata"}),
        final({"source_id": "S1", "quote": "22 working days … annual leave"}),
    ])
    result = await run(model, max_steps=2)
    assert "interleaved" in model.requests[2][-1].content
    assert result.status == "answered" and result.steps == 3 and result.sources[0]["quote"] == "22 working days … annual leave"


async def test_an_answer_without_a_citation_that_checks_out_is_not_found():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        final(),  # found yes, no citations: asked once to cite
        final({"source_id": "S1", "quote": "not in the document"}),
    ])
    result = await run(model)
    assert "no citations" in model.requests[2][-1].content
    assert result.status == "not_found" and result.sources == [] and result.steps == 3


@pytest.mark.parametrize("prompt", [agent.SYSTEM, agent.SYSTEM_SANDBOX])
def test_the_prompt_applies_a_region_only_where_the_documents_differ_by_region(prompt):
    assert "names no country or region applies to everyone" in prompt and "Don't add the user's region" in prompt
    assert "never from a document number alone" in prompt
    assert "PDF page N" in prompt  # the file's page, which can differ from the one printed on it


async def test_quotes_may_join_pieces_with_an_ellipsis():
    """Table cells aren't next to each other in extracted text; pieces in order, each verbatim, are a valid quote."""
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        final({"source_id": "S1", "quote": "Employees get … annual leave"},
              {"source_id": "S1", "quote": "annual leave ... Employees"}),  # out of order: not in the source
        final({"source_id": "S1", "quote": "Employees get … annual leave"}),
    ])
    result = await run(model)
    assert "annual leave ... Employees" in model.requests[2][-1].content
    assert result.sources[0]["quote"] == "Employees get … annual leave"


async def test_read_around_shows_the_neighbouring_passages_and_they_can_be_cited():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        reply(call("read_around", {"passage": "P1", "before": 1, "after": 1}, "a"),
              call("read_around", {"passage": "P9", "before": 1, "after": 1}, "b")),
        final({"source_id": "S1", "quote": "22 working days"}, {"source_id": "S1", "quote": "full-time staff only"}),
    ])
    result = await run(model)
    good, bad = model.requests[2][-2:]
    assert bad.status == "error" and "Unknown passage" in bad.content
    assert "P2" in good.content and "Table 3." in good.content and "pro rata" in good.content
    assert "22 working days of annual leave" not in good.content  # the passage asked about isn't repeated
    assert result.sources[0]["sections"] == ["leave-policy > Page 2", "leave-policy > Page 1"]


async def test_read_around_says_where_the_file_ends():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        reply(call("read_around", {"passage": "P1", "before": 0, "after": 3})),
        reply(call("read_around", {"passage": "P2", "before": 0, "after": 3})),  # P2 is the last passage
        final({"source_id": "S1", "quote": "22 working days"}),
    ])
    await run(model)
    assert "end of the file" not in model.requests[2][-1].content
    assert "end of the file" in model.requests[3][-1].content


async def test_read_and_list_use_registered_sources_only():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 3})),
        reply(call("read_file", {"source_id": "S1"}, "a"), call("read_file", {"source_id": "S5"}, "b")),
        reply(call("list_items", {"source_id": "S1"})),
        final({"source_id": "S1", "quote": "Carry over 5."}, answer="Carry over 5."),
    ])
    result = await run(model)
    good, bad = model.requests[2][-2:]
    assert good.status == "success" and bad.status == "error" and "Unknown source_id" in bad.content
    assert "S2" in model.requests[3][-1].content  # the listed sibling got an id
    assert result.sources[0]["quote"] == "Carry over 5." and "sections" not in result.sources[0]  # a whole file


async def test_answer_links_are_neutralised_and_unverified_citations_dropped():
    answer = reply(call("final_answer", {"found": True, "citations": [
        {"source_id": "S1", "quote": "22 working days"},
        {"source_id": "S2", "quote": ""},  # never read: can't be cited
    ], "answer": "22 days ![x](//evil.com/a.png?d=SECRET) <img src=//evil.com/b> evil.com/c?d=1 [y](javascript:alert(1))"}))
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        reply(call("list_items", {"source_id": "S1"})),  # registers S2 without reading it
        answer, answer,  # the same answer again after the fix round
    ])
    result = await run(model)
    assert [s["title"] for s in result.sources] == ["leave-policy.pdf"]
    assert "evil.com" not in result.answer and "javascript" not in result.answer and "<img" not in result.answer


async def test_not_found_is_a_valid_answer():
    model = FakeModel([final(found="no", answer="Nothing about that.")])
    result = await run(model)
    assert result.status == "not_found" and result.sources == []


async def test_an_answer_without_found_counts_as_found_when_a_citation_checks_out():
    """Gemini sometimes leaves out `found`; a cited answer must not then be reported as "not found"."""
    cited = reply(call("final_answer", {"answer": "22 days.", "citations": [{"source_id": "S1", "quote": "22 working days"}]}))
    model = FakeModel([reply(call("search", {"query": "leave", "limit": 5})), cited])
    assert (await run(model)).status == "answered"
    uncited = reply(call("final_answer", {"answer": "Nothing about that.", "citations": []}))
    assert (await run(FakeModel([uncited]))).status == "not_found"


async def test_a_question_answered_in_part_is_partial():
    model = FakeModel([
        reply(call("search", {"query": "leave", "limit": 5})),
        final({"source_id": "S1", "quote": "22 working days"}, found="partly",
              answer="22 days a year; the documents don't say how many can be carried over."),
    ])
    result = await run(model)
    assert result.status == "partial" and result.sources[0]["quote"] == "22 working days"


async def test_a_partial_answer_needs_a_citation_that_checks_out():
    bad = final({"source_id": "S9", "quote": "never retrieved"}, found="partly", answer="Maybe 30 days.")
    result = await run(FakeModel([bad, bad]))  # the second is the one fix round
    assert result.status == "not_found" and result.sources == [] and result.steps == 2


@pytest.mark.parametrize("found, status", [(True, "answered"), (False, "not_found"), ("Partly", "partial"), ("maybe", "answered")])
async def test_found_in_other_forms(found, status):
    """The older schema's booleans still work, case doesn't matter, and an unknown value goes by the citations."""
    model = FakeModel([reply(call("search", {"query": "leave", "limit": 5})),
                       final({"source_id": "S1", "quote": "22 working days"}, found=found)])
    assert (await run(model)).status == status


async def test_runs_out_of_steps_and_returns_what_it_found():
    model = FakeModel([reply(call("search", {"query": f"q{i}", "limit": 5})) for i in range(5)])
    result = await run(model, max_steps=3)
    assert result.status == "budget_exhausted" and result.steps == 3
    assert result.sources and result.sources[0]["link"] == "https://sp/leave"
    assert result.sources[0]["sections"] == ["leave-policy > Page 2"]
    nudge = model.requests[2][-1]
    assert isinstance(nudge, HumanMessage) and "final_answer" in nudge.content
    # The last step is asked as usual (the cache stays warm), then told it was the last, still with the usual tools;
    # it searched both times, so it is made to answer.
    assert model.tool_choices == [None, None, None, None, "final_answer"]
    assert model.requests[3][:-1] == model.requests[2] and model.requests[3][-1].content == agent.LAST_CALL
    assert [m.content for m in model.requests[4]] == [m.content for m in model.requests[3]]


async def test_a_last_step_told_it_was_the_last_answers_without_being_forced():
    model = FakeModel([reply(call("search", {"query": "leave", "limit": 5})),
                       reply(call("search", {"query": "more", "limit": 5})),
                       final({"source_id": "S1", "quote": "22 working days"})])
    result = await run(model, max_steps=2)
    assert result.status == "answered" and model.tool_choices == [None, None, None]
    assert (result.input_tokens, result.output_tokens) == (300, 150)  # every call is charged


async def test_the_last_step_answers_with_what_it_found():
    model = FakeModel([reply(call("search", {"query": "leave", "limit": 5})),
                       final({"source_id": "S1", "quote": "22 working days"}, found="partly")])
    result = await run(model, max_steps=2)
    assert result.status == "partial" and model.tool_choices == [None, None]  # answered when asked: not forced


async def test_partial_sources_are_neutralised_too():
    planted = hit(0, 'Leave is 22 days. ![x](https://evil.example/?d=secret) <a href="//evil.example/y">more</a> https://sp/notes',
                  heading=None, name="notes.md", web_url="https://sp/notes", item_id="j", source_id=8)

    async def search(q, k):
        return [planted]

    t = tools()
    t.search = search
    model = FakeModel([reply(call("search", {"query": f"q{i}", "limit": 5})) for i in range(4)])
    result = await run(model, t, max_steps=2)
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


class CapturingClient(Client):
    """A LangSmith client that keeps the runs it is sent instead of posting them."""

    def __init__(self):
        super().__init__(api_key="test", api_url="http://127.0.0.1:9", auto_batch_tracing=False)
        self.sent: dict[str, dict] = {}

    def create_run(self, name, inputs, run_type, **kw):
        self.sent.setdefault(str(kw["id"]), {}).update(name=name, inputs=inputs, run_type=run_type, **kw)

    def update_run(self, run_id, **kw):  # fields left out of an update (None) keep what the run was created with
        self.sent.setdefault(str(run_id), {}).update({k: v for k, v in kw.items() if v is not None})


async def test_trace_shows_the_question_the_result_and_each_tool_call():
    client = CapturingClient()
    model = FakeModel([reply(call("search", {"query": "annual leave", "limit": 5})), final({"source_id": "S1", "quote": "22 working days"})])
    with ls.tracing_context(enabled=True, client=client):
        await agent.ask(model, "How many leave days?", tools(), max_steps=6, max_tokens=100_000, timeout_seconds=30,
                        metadata={"user": "u1"})
    runs = list(client.sent.values())
    root = next(r for r in runs if r.get("parent_run_id") is None)
    assert root["name"] == "sharepoint_ask" and root["inputs"] == {"question": "How many leave days?"}
    assert root["outputs"]["status"] == "answered" and root["outputs"]["answer"] == "22 days."
    assert root["outputs"]["sources"][0]["quote"] == "22 working days"
    assert root["extra"]["metadata"]["user"] == "u1"
    search = next(r for r in runs if r["name"] == "search")
    assert search["run_type"] == "retriever" and search["inputs"] == {"query": "annual leave", "limit": 5}
    assert search["outputs"]["documents"][0]["page_content"] == HIT.text
    assert search["outputs"]["documents"][0]["metadata"]["section"] == "leave-policy > Page 2"
    assert not [r for r in runs if r["name"] == "route"]  # routing is done by the nodes, not traced as runs


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


def test_quotes_with_dot_leaders_still_match_whole():
    """Parts lists pad with dots; such a quote is checked whole first, not split at its "..."."""
    texts = [("Page 401", "2 PAHZZ 2520011491221 7X677 930679  .ROLLER ASSEMBLY,CLU................   1")]
    assert agent._quoted("930679 .ROLLER ASSEMBLY,CLU................ 1", texts) == (True, "Page 401")
    assert agent._quoted("PAHZZ … ROLLER ASSEMBLY", texts) == (True, "Page 401")
    assert agent._quoted("ROLLER ASSEMBLY … PAHZZ", texts) == (False, None)
    assert agent._quoted("…", texts) == (False, None)


def test_quotes_needn_t_copy_characters_nobody_can_see():
    """Extracted PDF text keeps control characters where bullets were; a model copying the text writes a space."""
    texts = [(None, "APPLICABLE MODELS EQUIPMENT CONDITION\nM815 W/W \x8d Parking brake set (TM 9-2320-260-10).\n"
                    "co­operate with the ﬁre crew")]
    assert agent._quoted("M815 W/W   Parking brake set (TM 9-2320-260-10).", texts)[0]
    assert agent._quoted("cooperate with the fire crew", texts)[0]  # soft hyphen, and the "ﬁ" ligature
    assert not agent._quoted("M815 W/W Parking brake released", texts)[0]


def test_tools_convert_for_real_providers():
    """The neutral tool schemas bind to a real LangChain provider (offline: no request is sent)."""
    # The converter ChatGoogleGenerativeAI applies to bound tools when it builds a request.
    from langchain_google_genai._function_utils import convert_to_genai_function_declarations

    declarations = convert_to_genai_function_declarations(agent.TOOLS)[0].function_declarations
    assert [d.name for d in declarations] == ["search", "read_around", "read_file", "list_items", "final_answer"]
    assert declarations[0].parameters.required == ["query", "limit"]
    assert declarations[1].parameters.required == ["passage", "before", "after"]
    assert declarations[4].parameters.properties["citations"].items.required == ["source_id", "quote"]
    assert list(declarations[4].parameters.properties["found"].enum) == ["yes", "partly", "no"]


# ---- the code interpreter (docs/code-interpreter.md) -------------------------------------------------------------

import subprocess

import httpx

from mcp_server import interpreter


class FakeSandbox:
    """Runs commands with bash in a folder per session: the agent's side of the protocol, no isolation (the sandbox
    service's own tests cover that)."""

    def __init__(self, root):
        self.root, self.uploads, self.commands, self.deleted = root, [], [], []

    async def upload(self, session_id, files):
        folder = self.root / session_id
        folder.mkdir(exist_ok=True)
        for name, content in files.items():
            (folder / name).write_text(content)
        self.uploads.append((session_id, dict(files)))

    async def execute(self, session_id, command, timeout):
        self.commands.append((session_id, command))
        p = subprocess.run(["bash", "-c", command], cwd=self.root / session_id, capture_output=True, text=True, timeout=timeout)
        return interpreter.Execution(p.stdout + p.stderr, 0, p.returncode, False)

    async def delete(self, session_id):
        self.deleted.append(session_id)


def manual(ordinal, text, page):
    return hit(ordinal, text, f"TM 9-2320-289-20 > Page {page}", source_id=9, name="TM 9-2320-289-20.pdf",
               web_url="https://sp/tm289", item_id="tm289")


# The M1008 fact search ranks 7th: in the file, but not among the hits shown. Page 56 starts mid-sentence.
MANUAL = [manual(0, "Engine oil service for the CUCV.", 55), manual(1, "Drain the oil. Crankcase", 55),
          manual(2, "capacity is 7 quarts with filter.", 56)]


def sandbox_tools(readable=("i", "tm289"), log=None):
    base = tools()

    async def search_files(q, k):
        return [HIT], [HIT, MANUAL[0]]

    async def read_files(files):
        if log is not None:
            log.append([f.item_id for f in files])
        whole = {7: AROUND, 9: MANUAL}
        return {f.source_id: whole[f.source_id] for f in files if f.item_id in readable}

    base.search_files, base.read_files = search_files, read_files
    return base


def run_call(command, id="r1"):
    return call("run", {"command": command}, id)


async def test_run_finds_a_fact_search_ranked_too_low_and_it_can_be_cited(tmp_path):
    sandbox = FakeSandbox(tmp_path)
    model = FakeModel([
        reply(call("search", {"query": "M1008 oil capacity", "limit": 5})),
        reply(run_call("grep -c -i crankcase S*.txt; cat manifest.tsv")),
        final({"source_id": "S2", "quote": "Crankcase capacity is 7 quarts with filter"}, answer="7 quarts."),
    ])
    result = await run(model, sandbox=sandbox)
    assert [t["name"] for t in model.bound_tools] == ["search", "run", "final_answer"]
    assert "one S<n>.txt per file" in model.requests[0][0].content
    # Search says where its hit is, not what it says, and names the file it added beyond its hits.
    found = json.loads(model.requests[1][-1].content)
    assert found == {"files": [{"source_id": "S1", "title": "leave-policy.pdf",
                                "hits": [{"lines": "4-4", "section": "Page 2",
                                          "match": "Employees get 22 working days of annual leave."}]}],
                     "other_files": "S2 TM 9-2320-289-20.pdf"}
    out = model.requests[2][-1].content
    assert "S1.txt:0\nS2.txt:1\n" in out and "S2\tTM 9-2320-289-20.pdf\n" in out
    # The hit's file is written at the search, so its lines can be read next; the others at the first run.
    (session_id, first), (_, second) = sandbox.uploads
    assert sorted(first) == ["S1.txt", "manifest.tsv"] and sorted(second) == ["S2.txt", "manifest.tsv"]
    assert first["S1.txt"].split("\n")[3] == "Employees get 22 working days of annual leave."  # line 4, as search said
    # Named by source id; markers per section; the overlap rebuilt once; the M1008 manual written although no hit.
    assert second["S2.txt"] == ("=== TM 9-2320-289-20 > Page 55 ===\nEngine oil service for the CUCV.\nDrain the oil. Crankcase\n"
                                "=== TM 9-2320-289-20 > Page 56 ===\ncapacity is 7 quarts with filter.\n")
    # The quote runs across the page break: it passes, and its page is the one it starts on.
    assert result.status == "answered"
    assert result.sources == [{"title": "TM 9-2320-289-20.pdf", "link": "https://sp/tm289",
                               "quote": "Crankcase capacity is 7 quarts with filter", "sections": ["TM 9-2320-289-20 > Page 55"]}]
    assert sandbox.deleted == [session_id]
    # The session id is the server's alone: not in anything the model was sent, nor in the result.
    assert all(session_id not in str(m.content) for request in model.requests for m in request)
    assert session_id not in str(result)


def test_a_hit_is_shown_as_its_lines_that_match_the_query():
    text = "Table 3-1. Capacities\nCooling system 24 quarts\nCrankcase oil capacity\n7 quarts with filter\nFuel 25 gal"
    assert agent._match(text, "M1008 crankcase oil capacity") == "Crankcase oil capacity"
    assert agent._match(text, "what is the coolant") == text.replace("\n", " ")[:agent.MATCH_CHARS]  # semantic hit
    assert agent._short_section("TM 9-2320-289-20 > Chapter 3 > Page 56") == "Chapter 3 > Page 56"


async def test_quotes_are_checked_against_the_servers_copy_never_command_output(tmp_path):
    model = FakeModel([
        reply(call("search", {"query": "oil", "limit": 5})),
        reply(run_call('echo "Crankcase capacity is 12 quarts"')),
        final({"source_id": "S2", "quote": "Crankcase capacity is 12 quarts"}, answer="12 quarts."),
        final({"source_id": "S2", "quote": "Crankcase capacity is 12 quarts"}, answer="12 quarts."),
    ])
    result = await run(model, sandbox=FakeSandbox(tmp_path))
    assert "Crankcase capacity is 12 quarts" in model.requests[2][-1].content  # the model saw it printed
    assert "not found word for word" in model.requests[3][-1].content
    assert result.sources == []  # dropped: the file never says 12


def test_quote_pieces_in_a_sandbox_file_must_be_near_each_other():
    far = interpreter.WorkFile([("Page 1", "Crankcase " + "filler " * 2000 + "oil capacity is"), ("Page 9", "12 quarts")])
    plain = agent._Plain.of(far)
    assert plain.find("Crankcase … 12 quarts") == (False, None)
    assert plain.find("oil capacity is … 12 quarts") == (True, "Page 1")  # across the break, on the page it starts
    assert plain.find("12 quarts") == (True, "Page 9")


def test_quotes_of_common_words_stay_cheap_in_a_large_file():
    plain = agent._Plain.of(interpreter.WorkFile([("Page 1", "the " * 2_000_000)]))
    started = time.monotonic()
    assert plain.find("the … zzzq") == (False, None)
    assert time.monotonic() - started < 1


async def test_run_output_is_capped_and_kept(tmp_path):
    model = FakeModel([
        reply(call("search", {"query": "oil", "limit": 5})),
        *[reply(run_call(f"echo probe {n}; seq 3", f"r{n}")) for n in range(4)],
        reply(run_call("yes line | head -c 10000", "big")),
        final({"source_id": "S2", "quote": "7 quarts"}),
    ])
    await run(model, sandbox=FakeSandbox(tmp_path), max_steps=10)
    sent = model.requests[-1]
    runs = [m for m in sent if isinstance(m, ToolMessage) and m.tool_call_id.startswith(("r", "big"))]
    # Old outputs are sent unchanged, so each request extends the last one and the provider's cache keeps working.
    assert [m.content for m in runs[:4]] == [f"probe {n}\n1\n2\n3\n" for n in range(4)]
    assert runs[4].content.endswith("\n[cut: 6,000 more characters; use grep -m or head]")
    assert len(runs[4].content) < agent.RUN_OUTPUT_CHARS + 100
    assert all(later[:len(earlier)] == earlier for earlier, later in zip(model.requests, model.requests[1:]))


async def test_files_are_written_when_needed_and_only_if_still_readable(tmp_path):
    log = []
    sandbox = FakeSandbox(tmp_path)
    no_run = FakeModel([reply(call("search", {"query": "oil", "limit": 5})), final({"source_id": "S1", "quote": "22 working days"})])
    await run(no_run, sandbox_tools(log=log), sandbox=sandbox)
    # The hit's file, for its lines; the files below it wait for a run, so none are built here.
    assert log == [["i"]] and [sorted(f) for _, f in sandbox.uploads] == [["S1.txt", "manifest.tsv"]]
    assert sandbox.deleted == [sandbox.uploads[0][0]]

    log.clear()
    model = FakeModel([
        reply(run_call("ls", "r0")),  # before any search
        reply(call("search", {"query": "oil", "limit": 5})),
        reply(run_call("ls", "r1")),
        reply(call("search", {"query": "oil again", "limit": 5})),
        reply(run_call("ls", "r2")),
        final({"source_id": "S1", "quote": "22 working days"}),
    ])
    await run(model, sandbox_tools(readable=("i",), log=log), sandbox=sandbox, max_steps=8)
    assert "search first" in model.requests[1][-1].content
    assert model.requests[3][-1].content == "S1.txt\nmanifest.tsv\n"  # the revoked manual is left out
    assert log == [["i"], ["tm289"]]  # each file is built and checked once, not again after the second search


async def test_run_is_refused_late_in_the_call(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "RUN_TIME_SHARE", 0.0)
    model = FakeModel([
        reply(call("search", {"query": "oil", "limit": 5})),
        reply(run_call("ls")),
        final({"source_id": "S1", "quote": "22 working days"}),
    ])
    sandbox = FakeSandbox(tmp_path)
    await run(model, sandbox=sandbox)
    last = model.requests[2][-1]
    assert last.status == "error" and "No time left" in last.content and sandbox.commands == []


async def test_sandbox_errors_never_show_the_session_id():
    session_id = "0123456789abcdef0123456789abcdef"

    def failing(request):
        if request.url.path.endswith("/files"):
            raise httpx.ConnectError(f"connection refused for {request.url}")
        return httpx.Response(500, json={"error": "internal"}, request=request)

    sandbox = interpreter.LocalSandbox("http://sandbox:8000", "key", transport=httpx.MockTransport(failing))
    for call_ in (sandbox.upload(session_id, {"S1.txt": "x"}), sandbox.execute(session_id, "ls", 5)):
        with pytest.raises(interpreter.SandboxError) as e:
            await call_
        assert session_id not in str(e.value) and "sandbox" in str(e.value)


async def test_large_uploads_are_split_and_a_failed_one_is_retried_once(tmp_path, monkeypatch):
    bodies = []

    def sandbox_api(request):
        if request.url.path.endswith("/files"):
            bodies.append(sorted(f["name"] for f in json.loads(request.content)["files"]))
            return httpx.Response(503, json={"error": "busy"}, request=request)
        return httpx.Response(200, json={"output": "ok", "omitted_bytes": 0, "exit_code": 0, "timed_out": False})

    sandbox = interpreter.LocalSandbox("http://sandbox:8000", "key", transport=httpx.MockTransport(sandbox_api))
    monkeypatch.setattr(interpreter, "UPLOAD_FILES", 2)
    model = FakeModel([
        reply(call("search", {"query": "oil", "limit": 5})),
        *[reply(run_call("ls", f"r{n}")) for n in range(3)],
        final({"source_id": "S1", "quote": "22 working days"}),
    ])
    await run(model, sandbox=sandbox, max_steps=8)
    # At the search, the hit's file; at the first run, both files (two to a request); then the one not yet tried
    # twice. Each failed batch stops the upload.
    assert bodies == [["S1.txt", "manifest.tsv"], ["S1.txt", "S2.txt"], ["S2.txt", "manifest.tsv"]]
    assert "Employees get 22 working days" in model.requests[1][-1].content  # the hit came back as a passage
    assert "503" in model.requests[2][-1].content and "503" in model.requests[3][-1].content
    assert "None of the files could be written" in model.requests[4][-1].content  # given up on: not tried again


async def test_a_sandbox_that_was_busy_once_still_gets_the_files(tmp_path):
    class Flaky(FakeSandbox):
        failed = False

        async def upload(self, session_id, files):
            if not self.failed:
                self.failed = True
                raise interpreter.SandboxError("The sandbox refused the request (429): too many sessions")
            await super().upload(session_id, files)

    model = FakeModel([
        reply(call("search", {"query": "oil", "limit": 5})),
        reply(run_call("ls", "r1")),
        final({"source_id": "S2", "quote": "7 quarts"}),
    ])
    result = await run(model, sandbox=Flaky(tmp_path))
    assert json.loads(model.requests[1][-1].content)["passages"][0]["text"].startswith("Employees")
    assert model.requests[2][-1].content == "S1.txt\nS2.txt\nmanifest.tsv\n"
    assert result.status == "answered"


async def test_tokens_of_an_unanswered_last_step_are_charged_when_the_forced_answer_fails():
    model = FakeModel([reply(call("search", {"query": "q", "limit": 5})),
                       reply(call("search", {"query": "again", "limit": 5})),
                       reply(call("search", {"query": "still", "limit": 5})), RuntimeError("provider down")])
    result = await run(model, max_steps=2)
    assert result.status == "error"
    assert (result.input_tokens, result.output_tokens) == (300, 150)  # the three calls that returned, each 100 + 50


async def test_a_last_step_text_answer_is_kept_when_asking_again_fails():
    model = FakeModel([reply(call("search", {"query": "q", "limit": 5})),
                       reply(stop="end_turn", text="Employees get 25 leave days."), RuntimeError("provider down")])
    result = await run(model, max_steps=2)
    assert (result.status, result.answer) == ("answered", "Employees get 25 leave days.")
    assert (result.input_tokens, result.output_tokens) == (200, 100)  # each call that returned, once


def test_work_file_lines_point_at_each_chunk():
    hits = [hit(1, "Table 3. Oil\nCrankcase capacity", "TM > Page 55"), hit(2, "is 7 quarts\nwith filter", "TM > Page 55"),
            hit(3, "Coolant\n24 quarts", "TM > Page 56"), hit(4, "", None), hit(5, "Notes\nend", None)]
    work = interpreter.WorkFile.from_chunks(hits)
    lines = work.text.split("\n")
    for h in hits:
        first, last = work.lines[h.ordinal]
        assert "\n".join(lines[first - 1:last]).endswith(h.text.split("\n")[-1]), h.ordinal
        assert h.text.split("\n")[0] in lines[first - 1]


async def test_files_whose_upload_timed_out_are_tried_again(tmp_path):
    class Slow(FakeSandbox):
        calls = 0

        async def upload(self, session_id, files):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10)  # cancelled by the call's deadline
            await super().upload(session_id, files)

    loop = agent.Loop(sandbox_tools(), Slow(tmp_path))
    loop.deadline = time.monotonic() + 0.2
    out = json.loads(await loop.run_tool("search", {"query": "oil", "limit": 5}))
    assert out["passages"][0]["source_id"] == "S1"  # timed out: the hit came back with its text
    assert ("d", "i") in loop._offered  # not taken for written
    loop.deadline = time.monotonic() + 10
    assert await loop.run_tool("run", {"command": "ls"}) == "S1.txt\nS2.txt\nmanifest.tsv\n"


async def test_run_works_over_the_files_there_when_new_ones_fail(tmp_path):
    class FailsLater(FakeSandbox):
        async def upload(self, session_id, files):
            if self.uploads:
                raise interpreter.SandboxError("The sandbox refused the request (507): full")
            await super().upload(session_id, files)

    loop = agent.Loop(sandbox_tools(), FailsLater(tmp_path))
    await loop.run_tool("search", {"query": "oil", "limit": 5})  # writes S1, the hit's file
    out = await loop.run_tool("run", {"command": "ls"})  # S2 fails
    assert out.startswith("S1.txt\nmanifest.tsv\n") and "new files not written yet" in out and "507" in out


async def test_a_revoked_sign_in_during_a_search_stops_the_run(tmp_path):
    t = sandbox_tools()

    async def read_files(files):
        raise PermissionError("token revoked")

    t.read_files = read_files
    with pytest.raises(PermissionError):
        await agent.Loop(t, FakeSandbox(tmp_path)).run_tool("search", {"query": "oil", "limit": 5})


async def test_a_plain_text_reply_before_anything_was_looked_up_is_not_nudged():
    model = FakeModel([AIMessage(content="I can't help with that.")])
    result = await run(model)
    assert result.status == "answered" and result.sources == [] and result.steps == 1 and len(model.requests) == 1
