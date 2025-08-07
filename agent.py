import asyncio
from livekit.agents import Agent, AgentSession, JobContext, mcp, AutoSubscribe
from livekit import rtc
from common import logger, send_text_message, search_web

class UnifiedAgent(Agent):
    def __init__(self, mode="voice"):
        super().__init__(
            instructions="You are a voice and text assistant. Be helpful and friendly. You have access to web search functionality. Use the search_web tool for current information.",
            tools=[search_web],
        )
        self.mode = mode

async def agent_entrypoint(ctx: JobContext, mode: str):
    logger.info(f"{mode.capitalize()} agent entrypoint started")
    
    if mode == "voice":
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    else:
        await ctx.connect(auto_subscribe=False)
        
    logger.info(f"Connected to room for {mode} agent")

    agent = UnifiedAgent(mode=mode)
    session = AgentSession(
        llm=ctx.llm,
        tts=ctx.tts,
        stt=ctx.stt if mode == "voice" else None,
        vad=ctx.proc.userdata.get("vad") if mode == "voice" else None,
        max_tool_steps=5,
        allow_interruptions=True if mode == "voice" else False,
        mcp_servers=[
            mcp.MCPServerHTTP(
                url='https://mcp.composio.dev/composio/server/34157b53-db3d-4f6b-89d6-9f2f7762ee84?transport=sse&connected_account_id=dd1ca81c-a9f3-4240-a6a5-e115e0994424&user_id=gmail-1492',
                timeout=10,
                client_session_timeout_seconds=10,
            ),
        ],
    )

    if mode == "text":
        def on_data_received(data: rtc.DataPacket):
            if data.topic == "lk.chat":
                message = data.data.decode('utf-8')
                logger.info(f"Received text message: {message}")
                asyncio.create_task(process_text_message(ctx.room, session, message))
        ctx.room.on("data_received", on_data_received)
        logger.info("Text message handler set up")

    await session.start(agent=agent, room=ctx.room)
    logger.info("Agent session started")
    
    await ctx.wait_for_participant()
    logger.info(f"Participant joined {mode} room")

    greeting = f"Hello! I'm your Groq {mode} assistant. How can I help you today?"
    await session.say(greeting, allow_interruptions=(mode == "voice"))
    await send_text_message(ctx.room, greeting)
    logger.info(f"Greeting sent for {mode} agent")

async def process_text_message(room: rtc.Room, session: AgentSession, message: str):
    try:
        logger.info(f"Processing text message: {message}")
        await send_text_message(room, "Processing your message...")
        speech_handle = session.generate_reply(user_input=message, tool_choice="auto")
        await speech_handle
        logger.info("Text message processed, voice response sent")
    except Exception as e:
        logger.error(f"Error processing text message: {e}")
        error_msg = "I encountered an error while processing your message. Please try again."
        await session.say(error_msg, allow_interruptions=False)
        await send_text_message(room, error_msg)

