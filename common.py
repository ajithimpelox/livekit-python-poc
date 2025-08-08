import asyncio
import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime
import os
from tavily import TavilyClient

from livekit.agents import (
    JobContext,
    WorkerOptions,
    cli,
    JobProcess,
    AutoSubscribe,
    Agent,
    AgentSession,
    function_tool,
    RunContext,
    ChatContext,
    ChatMessage,
)
from livekit.plugins import silero, groq
from livekit import rtc

from dotenv import load_dotenv

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


async def send_text_message(room: rtc.Room, message: str):
    """Send text message back to frontend for display purposes"""
    try:
        data = {
            "type": "chat_response",
            "message": message,
            "timestamp": int(datetime.now().timestamp() * 1000)
        }
        
        data_str = json.dumps(data)
        data_bytes = data_str.encode('utf-8')
        
        await room.local_participant.publish_data(
            payload=data_bytes,
            reliable=True,
            topic="lk.chat.response"
        )
        logger.info(f"Sent text message for display: {message[:50]}...")
        
    except Exception as e:
        logger.error(f"Failed to send text message: {e}")

