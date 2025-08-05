import asyncio
import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime

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
)
from livekit.agents.llm import (
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


class GroqVoiceTextAssistant(Agent):
    """Enhanced agent that handles both voice and text interactions using LiveKit v1.0"""
    
    def __init__(self, text_only: bool = False):
        if text_only:
            instructions = "You are the Groq text assistant with voice output. You interact with users through text messages and respond with both text and spoken audio. Be helpful, friendly, and provide clear, informative responses. You can search the web for current information when needed."
        else:
            instructions = "You are the Groq voice and text assistant. You can interact with users through both voice and text messages. Be helpful, friendly, and adapt your responses appropriately for the medium being used. When responding to text, be concise but informative. When responding to voice, be conversational and natural. You can search the web for current information when needed."
        
        super().__init__(
            instructions=instructions,
        )
        
        self.text_only = text_only
        self.conversation_history = []
        
        logger.info(f"Agent initialized in {'text-only' if text_only else 'voice'} mode")


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
        
        # Create the agent
        agent = GroqVoiceTextAssistant(text_only=is_text_mode)
        
        # Create AgentSession with Groq plugins
        session = AgentSession(
            vad=ctx.proc.userdata["vad"],
            stt=groq.STT(),
            llm=groq.LLM(),
            tts=groq.TTS(voice="Cheyenne-PlayAI"),
        )
        
        # Setup text message handler for both modes
        setup_text_handler(ctx.room, session, is_text_mode)
        
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
            greeting = "Hello! I'm your Groq text assistant. Send me a text message and I'll respond with both text and voice. How can I help you today?"
            await send_text_message(ctx.room, greeting)
            await session.say(greeting, allow_interruptions=False)
        else:
            greeting = "Hello! I'm your Groq voice assistant. How can I help you today?"
            await send_text_message(ctx.room, greeting)
            await session.say(greeting, allow_interruptions=True)
        
        logger.info("Agent session started successfully")
            
    except Exception as e:
        logger.error(f"Error in entrypoint: {e}")
        raise


def setup_text_handler(room: rtc.Room, session: AgentSession, is_text_mode: bool):
    """Setup text message handler using LiveKit's data_received event"""
    try:
        def on_data_received(data: rtc.DataPacket):
            try:
                if data.topic == "lk.chat":
                    message = data.data.decode('utf-8')
                    participant_identity = data.participant.identity if data.participant else "unknown"
                    
                    logger.info(f"Received text message from {participant_identity}: {message}")
                    
                    # Process the text message
                    asyncio.create_task(process_text_message(room, session, message, is_text_mode))
                    
            except Exception as e:
                logger.error(f"Error handling data received: {e}")
        
        room.on("data_received", on_data_received)
        logger.info("Text message handler setup complete")
        
    except Exception as e:
        logger.error(f"Failed to setup text handler: {e}")


async def process_text_message(room: rtc.Room, session: AgentSession, message: str, is_text_mode: bool):
    """Process incoming text messages"""
    try:
        logger.info(f"Processing text message: {message}")
        
        # Send loading indicator
        await send_text_message(room, "Processing your message...")
        
        # Create a chat context with the user message
        chat_ctx = ChatContext([ChatMessage(role="user", content=[message])])
        
        # Generate text response using the session's LLM
        llm = session.llm
        response_content = ""
        
        async for chunk in llm.chat(chat_ctx=chat_ctx):
            if chunk.delta and chunk.delta.content:
                response_content += chunk.delta.content
        
        # Send the text response back to the user
        if response_content.strip():
            await send_text_message(room, response_content.strip())
            
            # If in text mode, also speak the response
            if is_text_mode:
                await session.say(response_content.strip(), allow_interruptions=False)
        else:
            await send_text_message(room, "I apologize, but I couldn't generate a response. Please try again.")
        
        logger.info("Text message processed successfully")
        
    except Exception as e:
        logger.error(f"Error processing text message: {e}")
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