from livekit.agents import (
    JobContext,
    Agent,
    AgentSession,
    AutoSubscribe,
)
from common import logger, search_web, send_text_message
import asyncio

class VoiceToVoiceAgent(Agent):
    """An agent that listens to voice and responds with voice."""
    
    def __init__(self):
        super().__init__(
            instructions="You are the Groq voice assistant. Be helpful and friendly. IMPORTANT: You have access to web search functionality. ALWAYS use the search_web tool when users ask for current information, news, weather, prices, or any real-time data.",
            tools=[search_web],
        )

async def voice_agent_entrypoint(ctx: JobContext):
    """Entrypoint for the voice-to-voice agent, following the example's structure."""
    try:
        logger.info("Voice agent entrypoint started")
        
        # Using AutoSubscribe.AUDIO_ONLY as in the example
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        logger.info("Connected to room for voice agent")
        
        # Wait for participant before creating the session
        await ctx.wait_for_participant()
        logger.info("Participant joined voice room")
        
        agent = VoiceToVoiceAgent()
        
        session = AgentSession(
            vad=ctx.proc.userdata["vad"],
            stt=ctx.stt,
            llm=ctx.llm,
            tts=ctx.tts,
            max_tool_steps=5,
            allow_interruptions=True,
        )
        
        # Debugging listeners
        @session.on("transcript")
        def on_transcript(transcript: str):
            logger.info(f"Transcript received: {transcript}")

        @session.on("speech_start")
        def on_speech_start():
            logger.info("Speech started")

        @session.on("speech_end")
        def on_speech_end():
            logger.info("Speech ended")
            
        # Start the session, it will run in the background
        await session.start(agent=agent, room=ctx.room)
        logger.info("Agent session started")
        
        # Send greeting, allowing interruptions for a more natural feel
        greeting = "Hello! I'm your Groq voice assistant. How can I help you today?"
        await session.say(greeting, allow_interruptions=True)
        await send_text_message(ctx.room, greeting)
        logger.info("Greeting sent")
        
        logger.info("Voice agent is now active and listening.")
        # The entrypoint can now exit, the session will continue to run.
        
    except Exception as e:
        logger.error(f"Error in voice agent entrypoint: {e}", exc_info=True)
        raise
