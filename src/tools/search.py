import logging
import os
from langchain_core.tools import tool
from langchain_community.tools.tavily_search import TavilySearchResults
from src.config import TAVILY_MAX_RESULTS
from .decorators import create_logged_tool

logger = logging.getLogger(__name__)

if os.getenv("TAVILY_API_KEY"):
    # Initialize Tavily search tool with logging.
    LoggedTavilySearch = create_logged_tool(TavilySearchResults)
    tavily_tool = LoggedTavilySearch(name="tavily_search", max_results=TAVILY_MAX_RESULTS)
else:
    logger.warning("TAVILY_API_KEY is not set; tavily_search will return a configuration hint.")

    @tool("tavily_search")
    def tavily_tool(query: str) -> str:
        """Search the web with Tavily when TAVILY_API_KEY is configured."""
        return (
            "Tavily search is not configured. Set TAVILY_API_KEY in the AOE "
            "environment to enable real web search. Query was: " + query
        )
