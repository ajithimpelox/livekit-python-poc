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
async def search_web(context: RunContext, query: str) -> str:
    """Search the web for real-time information.
    
    Args:
        query: The search query string
        
    Returns:
        Search results from the web
    """
    logger.info(f"🔍 Searching the web for: {query}")
    try:
        # Initialize Tavily client
        tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY", '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'))
        response = tavily_client.search(query=query)
        
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

