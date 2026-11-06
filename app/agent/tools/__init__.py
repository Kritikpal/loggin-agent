"""The agent's tools in one place: name, how to build it for a project, calls allowed per question, and how its
earlier results are digested. The graph, the budget middleware, the refused-call note, the digest and the prompt all
read this, so adding or re-budgeting a tool is one line here.
"""

from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from app.agent.tools.count_by import BUDGET as COUNT_BUDGET
from app.agent.tools.count_by import make_count_by_tool
from app.agent.tools.find import BUDGET as FIND_BUDGET
from app.agent.tools.find import make_find_tool
from app.agent.tools.format import Digest, digest_count, digest_find, digest_overview, digest_search
from app.agent.tools.overview import BUDGET as OVERVIEW_BUDGET
from app.agent.tools.overview import make_overview_tool
from app.agent.tools.search_logs import BUDGET as SEARCH_BUDGET
from app.agent.tools.search_logs import make_search_logs_tool
from app.ingestion.projects import Project

MAX_TOOL_CALLS = 4  # tool calls that ran, per question, whatever the tools; the per-tool budgets add up to more


@dataclass(frozen=True)
class ToolSpec:
    name: str
    make: Callable[[Project], BaseTool]  # the tool for one project: it can only ever load that project's files
    budget: int  # calls that ran, per question
    digest: Digest  # an earlier result as the model sees it again; None = leave as is


TOOLS = (
    ToolSpec("overview", make_overview_tool, OVERVIEW_BUDGET, digest_overview),
    ToolSpec("find", make_find_tool, FIND_BUDGET, digest_find),
    ToolSpec("search_logs", make_search_logs_tool, SEARCH_BUDGET, digest_search),
    ToolSpec("count_by", make_count_by_tool, COUNT_BUDGET, digest_count),
)
BUDGETS = {spec.name: spec.budget for spec in TOOLS}
