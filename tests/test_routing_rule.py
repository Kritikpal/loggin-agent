"""The router's keyword pre-check. The one invariant: a message that asks for a change (or might) is never
shortcut to ANSWER, because a miss there would let a change request pass as a plain question."""

import pytest

from app.ai.routing import is_obviously_question

QUESTIONS = [
    "Hi",
    "Hello!",
    "Thanks!",
    "What can you do?",
    "How many units did we sell last week?",
    "Show me sales trends for jeans in September",
    "Which products are low on stock?",
    "Who are my top customers?",
    "Why did sales drop on Tuesday?",
    "What was revenue in August 2026?",
    "Compare this week with last week",
    "How is inventory looking for dresses?",
    "Is the jeans stock enough for next week?",
    "Find customers named Priya",
    "How do you calculate trends?",
]

CHANGES = [
    "Restock the low jeans",
    "Reorder 50 more of SKU-1",
    "Create a 15% campaign for jeans",
    "Run a 20% promotion on jeans",
    "Send a message to customers about the sale",
    "Email customers who bought jeans",
    "Delete the old campaign",
    "Update the price of dresses",
    "Change the reorder level",
    "Raise prices on jeans by 5%",
    "Apply a discount to slow items",
    "Put jeans on sale",
    "Mark down the slow items",
    "Set a 10% discount on shirts",
    "Offer free shipping on dresses",
    "Add a new product",
    "Launch the campaign",
    "Cancel it",
    "Make it 10%",
    "Do that",
    "yes",
    "ok go ahead",
    "No, don't",
    "Please proceed",
]

MIXED_OR_AMBIGUOUS = [
    "How are jeans doing? If slow, run a discount",
    "Check stock and reorder anything low",
    "What would a 10% discount do to sales?",
    "What is the price trend for jeans?",
]


def reply(text):
    return [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": text}]


def test_a_follow_up_to_a_reply_about_a_change_goes_to_the_router():
    refused = reply("I couldn't prepare that action (Missing: category). Nothing was changed.")
    offered = reply("Jeans are slow. I can prepare a campaign for your approval.")

    assert not is_obviously_question("The category is jeans", refused)
    assert not is_obviously_question("jeans, 15%", offered)
    assert is_obviously_question("The category is jeans", reply("Which category do you mean?"))


@pytest.mark.parametrize("message", QUESTIONS)
def test_plain_questions_skip_the_router(message):
    assert is_obviously_question(message)


@pytest.mark.parametrize("message", [*CHANGES, *MIXED_OR_AMBIGUOUS])
def test_anything_that_could_be_a_change_still_goes_to_the_router(message):
    assert not is_obviously_question(message)
