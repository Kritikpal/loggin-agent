from langchain_core.messages import AIMessage

from app.agent.middleware.leaks import LEAK_NOTE, strip_leak

LEAKED = ('Two errors (wso2 L158, 10:14:08Z).【assistant<|channel|>commentary to=functions.find '
          '<|constrain|>json<|message|>{"text":"x"}')


def test_leaked_markup_is_cut_off_and_noted():
    message = strip_leak(AIMessage(content=LEAKED))
    assert message.text == f"Two errors (wso2 L158, 10:14:08Z).\n\n{LEAK_NOTE}" and not message.tool_calls


def test_clean_answers_and_real_tool_calls_are_untouched():
    answer = AIMessage(content="Nothing relevant was found.")
    call = AIMessage(content="", tool_calls=[{"name": "find", "args": {}, "id": "1", "type": "tool_call"}])
    assert strip_leak(answer) is answer and strip_leak(call) is call
