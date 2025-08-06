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
    proc.userdata["vad"] = silero.VAD.load()


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


class GroqVoiceTextAssistant(Agent):
    """Enhanced agent that handles both voice and text interactions with web search capability"""
    
    def __init__(self, text_only: bool = False):
        if text_only:
            instructions = "You are the Groq text assistant with voice output. You interact with users through text messages and respond with both text and spoken audio. Be helpful, friendly, and provide clear, informative responses. IMPORTANT: You have access to web search functionality. ALWAYS use the search_web tool when users ask for current information, news, weather, prices, or any real-time data. Don't say you can't access real-time information - use the search_web tool!"
        else:
            instructions = "You are the Groq voice and text assistant. You can interact with users through both voice and text messages. Be helpful, friendly, and adapt your responses appropriately for the medium being used. IMPORTANT: You have access to web search functionality. ALWAYS use the search_web tool when users ask for current information, news, weather, prices, or any real-time data. Don't say you can't access real-time information - use the search_web tool!"
        
        # Pass the search_web tool to the Agent constructor
        super().__init__(
            instructions=instructions,
            tools=[search_web],
        )
        
        self.text_only = text_only
        logger.info(f"Agent initialized in {'text-only' if text_only else 'voice'} mode with web search tool")


async def entrypoint(ctx: JobContext):
    """Main entrypoint that supports both voice and text interactions"""
    try:
        # Detect connection mode based on room name
        room_name = ctx.job.room.name
        is_text_mode = room_name.startswith("text_")
        
        logger.info(f"Connecting to room... (UI Mode: {'text' if is_text_mode else 'voice'})")
        
        # Connect to the room
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        logger.info("Connected to room successfully")
        
        # Create the agent with web search tool
        agent = GroqVoiceTextAssistant(text_only=is_text_mode)
        
        # Create AgentSession with Groq plugins
        session = AgentSession(
            vad=ctx.proc.userdata["vad"],
            stt=groq.STT(),
            llm=groq.LLM(model="openai/gpt-oss-20b"),  # Use Llama 3.1 8B instant which is stable and supports tool calling
            tts=groq.TTS(voice="Cheyenne-PlayAI"),
            max_tool_steps=5,  # Increase tool steps limit to allow more web searches if needed
        )
        
        # Setup text message handler for both modes
        setup_text_handler(ctx.room, session, agent, is_text_mode)
        
        # Start the session
        await session.start(
            agent=agent,
            room=ctx.room,
        )
        
        # Wait for participant to join
        logger.info("Waiting for participant to join...")
        await ctx.wait_for_participant()
        logger.info("Participant connected, sending initial messages...")
        
        # Send initial greeting
        if is_text_mode:
            greeting = "Hello! I'm your Groq text assistant with web search capability. I can help you find current information, news, weather, prices, and much more. Send me a text message and I'll respond with both text and voice. How can I help you today?"
            await send_text_message(ctx.room, greeting)
            await session.say(greeting, allow_interruptions=False)
        else:
            greeting = "Hello! I'm your Groq voice assistant with web search capability. I can help you find current information, news, weather, prices, and much more. How can I help you today?"
            await send_text_message(ctx.room, greeting)
            await session.say(greeting, allow_interruptions=True)
        
        logger.info("Agent session started successfully")
            
    except Exception as e:
        logger.error(f"Error in entrypoint: {e}")
        raise


def setup_text_handler(room: rtc.Room, session: AgentSession, agent: Agent, is_text_mode: bool):
    """Setup text message handler using LiveKit's data_received event"""
    try:
        def on_data_received(data: rtc.DataPacket):
            try:
                if data.topic == "lk.chat":
                    message = data.data.decode('utf-8')
                    participant_identity = data.participant.identity if data.participant else "unknown"
                    
                    logger.info(f"Received text message from {participant_identity}: {message}")
                    
                    # Process the text message
                    asyncio.create_task(process_text_message(room, session, agent, message, is_text_mode))
                    
            except Exception as e:
                logger.error(f"Error handling data received: {e}")
        
        room.on("data_received", on_data_received)
        logger.info("Text message handler setup complete")
        
    except Exception as e:
        logger.error(f"Failed to setup text handler: {e}")



async def process_text_message(room: rtc.Room, session: AgentSession, agent: Agent, message: str, is_text_mode: bool):
    """Process text message using the agent's built-in tool handling"""
    try:
        logger.info(f"Processing text message: {message}")
        await send_text_message(room, "Processing your message...")

        # Use generate_reply with the correct parameter name
        response_content = ""
        
        # Generate reply using the correct parameter - user_input as string
        try:
            # Log the number of items in history before processing
            logger.info(f"Chat history before processing: {len(session.history.items)} items")
            
            speech_handle = session.generate_reply(user_input=message, tool_choice="auto")
            
            # Wait for the speech to complete and get the response
            await speech_handle
            
            # Log the number of items in history after processing
            logger.info(f"Chat history after processing: {len(session.history.items)} items")
            
            # Get the response from the chat history (last assistant message)
            if session.history.items:
                # Log the last few items for debugging
                for i, item in enumerate(reversed(session.history.items[:5])):
                    logger.info(f"History item {i}: type={item.type}, role={getattr(item, 'role', 'N/A')}, content_preview={str(getattr(item, 'content', 'N/A'))[:100]}...")
                
                # Find the last assistant message (skip the greeting message)
                assistant_messages = []
                for item in reversed(session.history.items):
                    if (item.type == "message" and 
                        item.role == "assistant" and 
                        item.content):
                        if isinstance(item.content, list):
                            # Join text content from the list
                            text_parts = [str(c) for c in item.content if isinstance(c, str)]
                            content = " ".join(text_parts) if text_parts else ""
                        else:
                            content = str(item.content)
                        assistant_messages.append(content)
                        logger.info(f"Found assistant message: {content[:100]}...")
                
                # Skip greeting messages and get the actual response
                for content in assistant_messages:
                    if not any(greeting in content.lower() for greeting in ["hello", "i'm your groq", "how can i help"]):
                        response_content = content
                        logger.info(f"Using non-greeting response: {response_content[:200]}...")
                        break
                
                # If no non-greeting response found, use the most recent one
                if not response_content and assistant_messages:
                    response_content = assistant_messages[0]
                    logger.info(f"Using most recent response: {response_content[:200]}...")
            
            if not response_content.strip():
                response_content = "Sorry, I couldn't generate a reply."
                logger.warning("No response content found, using fallback message")

            # Send text response back to chat
            await send_text_message(room, response_content.strip())
            
            # Note: The speech is handled by the AgentSession internally
            # We don't need to call session.say() again as generate_reply already handles TTS
            
            logger.info("Text message processed successfully")
            
        except Exception as reply_error:
            logger.error(f"Error generating reply: {reply_error}")
            error_message = "I encountered an error while processing your message. Please try again."
            await send_text_message(room, error_message)
            
            # Still try to provide voice feedback about the error
            try:
                await session.say(error_message, allow_interruptions=False)
            except Exception as voice_error:
                logger.error(f"Error generating error voice response: {voice_error}")

    except Exception as e:
        logger.error(f"Error processing text message: {e}")
        import traceback
        traceback.print_exc()
        await send_text_message(room, f"Failed to process message: {str(e)}")


async def send_text_message(room: rtc.Room, message: str):
    """Send text message back to frontend"""
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
        logger.info(f"Sent text message: {message[:50]}...")
        
    except Exception as e:
        logger.error(f"Failed to send text message: {e}")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="groq-enhanced-agent",
        )
    )