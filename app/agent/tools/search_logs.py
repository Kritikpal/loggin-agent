"""search_logs: events similar in meaning, when the exact words are unknown."""

import logging
from datetime import datetime

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.tools.common import TIME_HELP, time_window, tool, validation_text
from app.agent.tools.format import Row, assemble, coverage_line, event_rows, row_text, short
from app.core.database import SessionLocal
from app.core.embeddings import embed_query
from app.ingestion.projects import Project
from app.ingestion.sources import Source
from app.schemas.search_logs import SearchLogsRequest, SearchLogsResponse
from app.search.hybrid import search_logs

logger = logging.getLogger(__name__)

# Every result is sent again with each later model call, so hits are few and short.
TOP_K = 5  # hits shown for a search without a level filter, whatever top_k says
TOP_K_FILTERED = 10  # a level filter is how the model lists every ERROR or WARN
HITS_TEXT = 4000  # total characters of hit messages, shared between the hits
MIN_MESSAGE = 450  # never less: a WSO2 error line is 417 characters and its user id sits at character 334
MAX_MESSAGE = 800
MAX_CAUSE = 200
FILTER_ONLY_QUERY = "error"  # stands in when the model searches by filter alone; the filters do the narrowing

DESCRIPTION = """Events similar in meaning, when the exact words are unknown. Shows at most 5 hits, 10 with `levels`: a sample, never count them. `query`: an identifier, plain words or both; no quotes or operators. `by`: k keyword, s semantic match."""


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No min_length / le: the provider rejects those before our code runs; run() clamps instead.
    query: str = Field(max_length=2000, description="Search text")
    top_k: int = Field(default=TOP_K, ge=1)
    start_time: datetime | None = Field(default=None, description=TIME_HELP)
    end_time: datetime | None = Field(default=None, description=TIME_HELP)
    levels: list[str] | None = Field(default=None, description="e.g. ERROR, WARN")


def make_search_logs_tool(project: Project) -> StructuredTool:
    sources = project.files  # only the project's files: that is what keeps a chat inside its project

    async def run(**arguments) -> str:
        requested = arguments.get("top_k", TOP_K)
        cap = TOP_K_FILTERED if arguments.get("levels") else TOP_K
        start, end = time_window(arguments, sources)
        try:
            request = SearchLogsRequest(
                query=arguments["query"].strip() or FILTER_ONLY_QUERY, top_k=min(requested, cap),
                start_time=start, end_time=end, levels=arguments.get("levels"))
        except ValidationError as exc:
            raise ToolException(validation_text(exc)) from exc
        try:
            async with SessionLocal() as session:
                response = await search_logs(request, session, sources=sources, embed=embed_query)
        except Exception:
            logger.exception("search_logs tool failed")
            return "error: search is temporarily unavailable"
        note = f"showing the top {cap} of the {requested} requested hits" if requested > cap else None
        return compact_hits(response, project.id, note, sources)

    return tool(run, "search_logs", DESCRIPTION, SearchArgs)


def compact_hits(response: SearchLogsResponse, project_id: str, note: str | None, sources: tuple[Source, ...]) -> str:
    """Hits as find-style rows plus their sig, so a good hit can be followed by find(sig=...)."""
    by_id = {s.id: s for s in sources}
    message_max = max(MIN_MESSAGE, min(MAX_MESSAGE, HITS_TEXT // max(len(response.results), 1)))
    rows = [
        Row(file=short(hit.source_id, project_id), occurred_at=hit.occurred_at, line=hit.locator.get("line_start"),
            level=hit.level, sig=hit.signature, extra="".join(leg[0] for leg in hit.matched_by),
            text=row_text(hit.raw_text, by_id.get(hit.source_id), limit=message_max, cause=MAX_CAUSE))
        for hit in response.results
    ]
    header, lines = event_rows(rows, "time line level sig by text", total_label="hits", total=len(rows))
    return assemble(header, lines, note, coverage_line(response.coverage, project_id))
