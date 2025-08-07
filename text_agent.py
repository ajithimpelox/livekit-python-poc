import asyncio
from livekit.agents import Agent, AgentSession, JobContext
from livekit import rtc

from common import logger, send_text_message, search_web

class TextToVoiceAgent(Agent):
    """An agent that receives text and responds with voice."""
    
    def __init__(self):
        super().__init__(
            instructions="You are a voice assistant that receives text input and responds with audio. Be helpful and friendly. IMPORTANT: You have access to web search functionality. ALWAYS use the search_web tool when users ask for current information, news, weather, prices, or any real-time data.",
            tools=[search_web],
        )

async def text_agent_entrypoint(ctx: JobContext):
    """Entrypoint for the text-to-voice agent."""
    logger.info("Text agent entrypoint started")
    
    # Connect to the room
    await ctx.connect(auto_subscribe=False)
    logger.info("Connected to room for text agent")
    
    agent = TextToVoiceAgent()
    session = AgentSession(
        llm=ctx.llm,
        tts=ctx.tts,
        max_tool_steps=5,
    )

    # Set up text message handler
    def on_data_received(data: rtc.DataPacket):
        if data.topic == "lk.chat":
            message = data.data.decode('utf-8')
            participant_identity = data.participant.identity if data.participant else "unknown"
            logger.info(f"Received text message from {participant_identity}: {message}")
            
            # Process the message and generate a proper response
            asyncio.create_task(process_text_message(ctx.room, session, message))

    ctx.room.on("data_received", on_data_received)
    logger.info("Text message handler set up")
    
    await session.start(agent=agent, room=ctx.room)
    logger.info("Text agent session started")
    
    # Wait for participant to join
    await ctx.wait_for_participant()
    logger.info("Participant joined text room")
    
    # Send greeting
    greeting = "Hello! I'm your Groq text assistant. You can send me text messages, and I'll respond with my voice. How can I help you today?"
    await session.say(greeting, allow_interruptions=False)
    await send_text_message(ctx.room, greeting)
    logger.info("Greeting sent for text agent")

async def process_text_message(room: rtc.Room, session: AgentSession, message: str):
    """Process a text message and generate a voice response."""
    try:
        logger.info(f"Processing text message: {message}")
        
        # Send processing indicator
        await send_text_message(room, "Processing your message...")
        
        # Generate reply using the agent's LLM and TTS
        speech_handle = session.generate_reply(user_input=message, tool_choice="auto")
        await speech_handle
        
        logger.info("Text message processed, voice response sent")
        
    except Exception as e:
        logger.error(f"Error processing text message: {e}")
        error_msg = "I encountered an error while processing your message. Please try again."
        await session.say(error_msg, allow_interruptions=False)
        await send_text_message(room, error_msg)

