"""Lane-scoped toolsets (plan §4.4 table). Each researcher subgraph run gets only its
lane's tools — context isolation, not just organization: a people-lane researcher should
never be tempted to call edgar_search."""
from __future__ import annotations

from app.tools.edgar import edgar_search, edgar_submissions
from app.tools.gdelt import news_search
from app.tools.linkedin import linkedin_company_profile, linkedin_jobs, linkedin_lookup, linkedin_people_profile
from app.tools.serper import fetch_page, web_search
from app.tools.propublica import nonprofit_lookup
from app.tools.think import think_tool

LANE_TOOLS = {
    # linkedin_company_profile corroborates identity/type (industry, size, founding year)
    "identity_and_type": [
        edgar_search, edgar_submissions, web_search, fetch_page, nonprofit_lookup,
        linkedin_company_profile, think_tool,
    ],
    # linkedin_lookup (tier 1 SERP x-ray) finds a candidate profile URL;
    # linkedin_people_profile pulls full detail off it once found.
    "people": [web_search, fetch_page, linkedin_lookup, linkedin_people_profile, think_tool],
    # linkedin_jobs' hiring-postings signal is a recency/signs-of-life indicator.
    "activity_signals": [news_search, web_search, linkedin_jobs, think_tool],
}

__all__ = [
    "LANE_TOOLS",
    "edgar_search",
    "edgar_submissions",
    "news_search",
    "linkedin_lookup",
    "linkedin_people_profile",
    "linkedin_company_profile",
    "linkedin_jobs",
    "fetch_page",
    "web_search",
    "nonprofit_lookup",
    "think_tool",
]
