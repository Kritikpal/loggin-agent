"""The agent's tools in one place: name, how to build it for a project, calls allowed per question, and how its
earlier results are digested (spec 2026-11-03). The graph, the budget middleware, the refused-call note, the digest
and the prompt all read this, so adding or re-budgeting a tool is one line here.
"""

from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from app.agent.find import make_find_tool
from app.agent.overview import make_overview_tool
from app.agent.rows import digest_find, digest_overview, digest_search
from app.agent.tools import make_search_logs_tool
from app.ingestion.projects import Project

MAX_TOOL_CALLS = 4  # tool calls that ran, per question, whatever the tools; the per-tool budgets add up to more


@dataclass(frozen=True)
class ToolSpec:
    name: str
    factory: Callable[[Project], BaseTool]  # the tool for one project: it can only ever load that project's files
    budget: int  # calls that ran, per question
    digest: Callable[[str], str | None]  # an earlier result as the model sees it again; None = leave as is


def _scoped(make: Callable[..., BaseTool]) -> Callable[[Project], BaseTool]:
    def build(project: Project) -> BaseTool:
        files = project.files
        return make(project_id=project.id, load_sources=lambda: files)
    return build


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("overview", _scoped(make_overview_tool), 1, digest_overview),
    ToolSpec("find", _scoped(make_find_tool), 2, digest_find),
    ToolSpec("search_logs", _scoped(make_search_logs_tool), 2, digest_search),
)
BUDGETS = {spec.name: spec.budget for spec in TOOLS}
