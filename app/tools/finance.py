"""Market data tools: Alpha Vantage (fundamentals, earnings, quotes) and Yahoo Finance news."""

import asyncio
from typing import Literal

import httpx
from langchain_core.tools import tool

from app.config import settings
from app.permissions.service import require

AV_URL = "https://www.alphavantage.co/query"


async def _alpha_vantage(function: str, **params) -> dict:
    require("finance:read")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            AV_URL, params={"function": function, "apikey": settings.alpha_vantage_api_key, **params}
        )
        response.raise_for_status()
        data = response.json()
    if "Note" in data or "Information" in data:
        raise RuntimeError(data.get("Note") or data.get("Information"))
    return data


@tool
async def search_symbol(keywords: str) -> dict:
    """Find ticker symbols by company name or partial ticker."""
    return await _alpha_vantage("SYMBOL_SEARCH", keywords=keywords)


@tool
async def get_quote(symbol: str) -> dict:
    """Latest price, change and volume for a ticker."""
    return await _alpha_vantage("GLOBAL_QUOTE", symbol=symbol)


@tool
async def get_company_overview(symbol: str) -> dict:
    """Company profile, valuation ratios and key metrics."""
    return await _alpha_vantage("OVERVIEW", symbol=symbol)


@tool
async def get_financial_statement(
    symbol: str, statement: Literal["INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW"]
) -> dict:
    """Annual and quarterly income statement, balance sheet or cash-flow statement."""
    return await _alpha_vantage(statement, symbol=symbol)


@tool
async def get_earnings(symbol: str) -> dict:
    """Historical annual/quarterly EPS with analyst estimates and surprises."""
    return await _alpha_vantage("EARNINGS", symbol=symbol)


@tool
async def get_earnings_estimates(symbol: str) -> dict:
    """Forward EPS and revenue estimates with analyst counts and revisions."""
    return await _alpha_vantage("EARNINGS_ESTIMATES", symbol=symbol)


@tool
async def get_earnings_call_transcript(symbol: str, year: int, quarter: Literal["Q1", "Q2", "Q3", "Q4"]) -> dict:
    """Full earnings-call transcript for a fiscal quarter, with sentiment signals."""
    return await _alpha_vantage("EARNINGS_CALL_TRANSCRIPT", symbol=symbol, quarter=f"{year}{quarter}")


@tool
async def get_news_sentiment(symbol: str) -> dict:
    """Recent news articles about a ticker with sentiment scores."""
    return await _alpha_vantage("NEWS_SENTIMENT", tickers=symbol, limit=20)


@tool
async def get_yahoo_news(symbol: str) -> list[dict]:
    """Latest Yahoo Finance headlines for a ticker (no API key needed)."""
    require("finance:read")
    import yfinance

    items = await asyncio.to_thread(lambda: yfinance.Ticker(symbol).news or [])
    news = []
    for item in items[:10]:
        content = item.get("content", item)
        news.append(
            {
                "title": content.get("title"),
                "summary": content.get("summary"),
                "published": content.get("pubDate") or content.get("providerPublishTime"),
                "url": (content.get("canonicalUrl") or {}).get("url") or content.get("link"),
            }
        )
    return news


ALPHA_VANTAGE_TOOLS = [
    search_symbol,
    get_quote,
    get_company_overview,
    get_financial_statement,
    get_earnings,
    get_earnings_estimates,
    get_earnings_call_transcript,
    get_news_sentiment,
]


def finance_tools() -> list:
    tools = [get_yahoo_news]
    if settings.alpha_vantage_api_key:
        tools = ALPHA_VANTAGE_TOOLS + tools
    return tools
