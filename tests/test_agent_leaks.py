"""gpt-oss sometimes writes a tool call into its answer text instead of making it. The middleware turns it back into a
real tool call, or strips it with a note. The two samples below are verbatim from eval runs (t2 and q5)."""

from langchain_core.messages import AIMessage

from app.agent.leaks import LEAK_NOTE, recover

T2 = ('Number of ERROR events: **18** (summarized from the log index).【assistant<|channel|>commentary '
      'to=functions.summarize_logs <|constrain|>json<|message|>{"levels":["ERROR"],"source_ids":["wso2-carbon-0114"]}')
Q5 = ('(source dev-auth-service, line 158, 2026-03-10T10:14:08.169Z)【assistant to=functions.search_logs<|channel|>'
      'commentary <|constrain|>json<|message|>{"source_ids":["dev-auth-service"],"query":"Failed to initialize JPA '
      'EntityManagerFactory","top_k":5}')
TOOLS = {"search_logs", "summarize_logs"}


def test_a_leaked_call_becomes_a_real_tool_call_and_the_text_before_it_is_kept():
    message = recover(AIMessage(content=T2), TOOLS)

    (call,) = message.tool_calls
    assert (call["name"], call["args"]) == ("summarize_logs", {"levels": ["ERROR"], "source_ids": ["wso2-carbon-0114"]})
    assert message.text == "Number of ERROR events: **18** (summarized from the log index)."


def test_the_other_ordering_of_the_markup_is_recovered_too():
    (call,) = recover(AIMessage(content=Q5), TOOLS).tool_calls

    assert call["name"] == "search_logs" and call["args"]["query"] == "Failed to initialize JPA EntityManagerFactory"


def test_a_leak_naming_an_unknown_tool_is_stripped_and_noted_never_run():
    leaked = Q5.replace("functions.search_logs", "functions.delete_logs")

    message = recover(AIMessage(content=leaked), TOOLS)

    assert not message.tool_calls
    assert "<|" not in message.text and "【" not in message.text and message.text.endswith(LEAK_NOTE)


def test_unreadable_arguments_are_stripped_and_noted():
    message = recover(AIMessage(content=T2.replace('{"levels"', '{levels')), TOOLS)

    assert not message.tool_calls and message.text.endswith(LEAK_NOTE)


def test_a_normal_answer_and_a_real_tool_call_are_left_alone():
    answer = AIMessage(content="Root cause: SPRING_DATASOURCE_URL is unset (L158).")
    call = AIMessage(content="", tool_calls=[{"name": "search_logs", "args": {"query": "x"}, "id": "c1",
                                              "type": "tool_call"}])

    assert recover(answer, TOOLS) is answer
    assert recover(call, TOOLS) is call
