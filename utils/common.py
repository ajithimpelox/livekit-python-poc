import json
import logging
from typing import Optional
from datetime import datetime
import os
from tavily import TavilyClient

from livekit.agents import (
    JobProcess,
    function_tool,
    RunContext,
    ChatMessage,
)
from livekit.plugins import silero
from livekit import rtc

from dotenv import load_dotenv
from tools.rag_tools import get_rag_information_from_vector_store

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def prewarm(proc: JobProcess):
    logger.info("=== PREWARM FUNCTION CALLED ===")
    try:
        proc.userdata["vad"] = silero.VAD.load()
        logger.info("VAD loaded successfully in prewarm")
    except Exception as e:
        logger.error(f"Error in prewarm: {e}")
        raise


# Minimal runtime container used by tools. Values may remain None if not configured.
_RUNTIME: dict = {
    "room": None,
    "namespace": None,
    "index_name": None,
    "customer_id": None,
}

# Define web search tool outside the class
@function_tool()
async def search_web(context: RunContext, query: Optional[str] = None) -> str:
    """Search the web for real-time information.
    
    Args:
        query: The search query string. If not provided, derives from the latest user message.
        
    Returns:
        Search results from the web
    """
    # Derive query from latest user message if missing
    effective_query = (query or "").strip()
    if not effective_query:
        try:
            # Look back through chat items on this turn for the last user message
            for item in reversed(context.speech_handle.chat_items):
                if isinstance(item, ChatMessage) and item.role == "user":
                    text = item.text_content or ""
                    if text:
                        effective_query = text
                        break
        except Exception as _:
            effective_query = ""

    # Heuristic: strip email instruction fragments from the query to improve search quality
    if effective_query:
        try:
            import re
            effective_query = re.sub(r"\band send to mail id\b.*", "", effective_query, flags=re.I).strip()
            effective_query = re.sub(r"\bsend to (?:my )?email\b.*", "", effective_query, flags=re.I).strip()
        except Exception:
            pass

    if not effective_query:
        return "Please specify what to search for."

    logger.info(f"🔍 Searching the web for: {effective_query}")
    try:
        # Initialize Tavily client
        tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY", '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'))
        response = tavily_client.search(query=effective_query)
        
        # Format the results nicely
        if isinstance(response, dict) and 'results' in response and response['results']:
            formatted_results = []
            for i, result in enumerate(response['results'][:3], 1):  # Top 3 results
                title = result.get('title', 'No title')
                content = result.get('content', 'No content')[:200] + "..."
                url = result.get('url', '')
                formatted_results.append(f"{i}. **{title}**\n   {content}\n   Source: {url}")
            
            return "Here's what I found:\n\n" + "\n\n".join(formatted_results)
        else:
            return "I searched but couldn't find relevant results for your query."
            
    except Exception as e:
        logger.error(f"Error performing web search: {e}")
        return f"I encountered an error while searching: {str(e)}"


@function_tool()
async def search_knowledge_base(context: RunContext, query: Optional[str] = None) -> str:
    """Search the internal knowledge base for relevant information related to the user's query."""
    # Derive query from latest user message if missing
    effective_query = (query or "").strip()
    if not effective_query:
        try:
            for item in reversed(context.speech_handle.chat_items):
                if isinstance(item, ChatMessage) and item.role == "user":
                    text = item.text_content or ""
                    if text:
                        effective_query = text
                        break
        except Exception:
            effective_query = ""

    if not effective_query:
        return "Please specify what to search for in the knowledge base."

    # Notify frontend if possible
    try:
        if _RUNTIME.get("room") is not None:
            await send_text_message(_RUNTIME["room"], "message", "Searching knowledge base...")
    except Exception as e:
        logger.warning(f"Failed to notify frontend about KB search: {e}")

    namespace = _RUNTIME.get("namespace")
    index_name = _RUNTIME.get("index_name")
    if not namespace or not index_name:
        return (
            "Knowledge base is not configured yet. I'll answer with general knowledge instead."
        )

    try:
        kb = await get_rag_information_from_vector_store(
            namespace=namespace, index_name=index_name, message=effective_query, top_k=1
        )
        results = (kb or {}).get("results") or []
        if results:
            # results is list of tuples: (Document, score)
            doc = results[0][0] if results[0] and len(results[0]) > 0 else None
            page_content = getattr(doc, "page_content", "") or getattr(doc, "pageContent", "") or ""
            page_number = None
            try:
                page_number = getattr(doc, "metadata", {}).get("page")
            except Exception:
                try:
                    page_number = doc.metadata.get("page") if hasattr(doc, "metadata") else None
                except Exception:
                    page_number = None
            logger.info(f"Page no: {page_number}")
            # Send page number to frontend if available
            if _RUNTIME.get("room") is not None and page_number not in (None, ""):
                try:
                    await send_text_message(
                        _RUNTIME["room"],
                        "presentation-page-number",
                        "",
                        {"pageNumber": page_number},
                    )
                except Exception as e:
                    logger.warning(f"Failed to send page number to frontend: {e}")

            if page_content:
                return (
                    "Based on your query, here's what I found in the knowledge base:\n\n"
                    + page_content
                )

        return (
            f"I couldn't find specific information about \"{effective_query}\" in the knowledge base. "
            "I'll help you with my general knowledge instead."
        )
    except Exception as e:
        customer_id = _RUNTIME.get("customer_id")
        logger.error(
            "Knowledge base search failed",
            extra={
                "error": str(e),
                "query": effective_query,
                "customer_id": customer_id,
                "namespace": namespace,
                "index_name": index_name,
            },
        )
        return (
            f"I apologize, but I'm having trouble accessing the knowledge base right now. "
            f"I'll help you with my general knowledge about \"{effective_query}\"."
        )


@function_tool()
async def store_long_term_memory_information(
    context: RunContext, key: str, value: str
) -> str:
    """Store important information as a key-value pair for future conversations."""
    customer_id = _RUNTIME.get("customer_id")
    print(f"Customer ID: {customer_id}")
    print(f"Key: {key}")
    print(f"Value: {value}")
    try:
        # Notify frontend
        if _RUNTIME.get("room") is not None:
            try:
                await send_text_message(
                    _RUNTIME["room"], "message", "Storing information in long term memory..."
                )
            except Exception as e:
                logger.warning(f"Failed to notify frontend about memory storage: {e}")

        # Local import to avoid circular import at module load time
        from database.db_queries import upsert_customer_realtime_information

        await upsert_customer_realtime_information(customer_id=customer_id, key=key, value=value)
        return "Successfully stored information in long term memory"
    except Exception as e:
        logger.error(
            "Failed to store long term memory information",
            extra={"error": str(e), "customer_id": customer_id, "key": key},
        )
        return f"Failed to store information: {str(e)}"


async def send_text_message(room: rtc.Room, topic: str, message: str, additional_data: dict = None):
    """Send text message back to frontend for display purposes"""
    try:
        data = {
            "topic": topic,
            "message": message,
            "timestamp": int(datetime.now().timestamp() * 1000),
        }
        if additional_data:
            data.update(additional_data)
            
        data_str = json.dumps(data)
        data_bytes = data_str.encode('utf-8')
        
        if getattr(room, "local_participant", None):
            await room.local_participant.publish_data(
              payload=data_bytes,
              reliable=True,
              topic=topic
            )
            logger.info(f"Sent text message for display: {message[:50]}...")
        
    except Exception as e:
        logger.error(f"Failed to send text message: {e}")

