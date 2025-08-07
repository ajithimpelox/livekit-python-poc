import logging
import sys
import os
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.plugins import groq

# Set up logging first
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    from common import prewarm, logger as common_logger
    from text_agent import text_agent_entrypoint
    from voice_agent import voice_agent_entrypoint
    logger.info("All imports successful")
except ImportError as e:
    logger.error(f"Import error: {e}")
    sys.exit(1)

async def entrypoint(ctx: JobContext):
    """Router entrypoint that decides which agent to run."""
    try:
        room_name = ctx.job.room.name
        logger.info(f"=== ENTRYPOINT CALLED === Starting agent for room: {room_name}")
        logger.info(f"Job ID: {ctx.job.id}")
        logger.info(f"Room ID: {ctx.job.room.sid}")
        
        # Initialize plugins
        logger.info("Initializing plugins...")
        ctx.llm = groq.LLM(model="llama3-8b-8192")
        ctx.tts = groq.TTS(voice="Cheyenne-PlayAI")
        ctx.stt = groq.STT()
        # VAD is initialized in prewarm and stored in ctx.proc.userdata["vad"]
        
        logger.info("Plugins initialized successfully")

        if room_name.startswith("text_"):
            logger.info("=== LAUNCHING TEXT AGENT ===")
            await text_agent_entrypoint(ctx)
        else:
            logger.info("=== LAUNCHING VOICE AGENT ===")
            await voice_agent_entrypoint(ctx)
            
        logger.info("=== ENTRYPOINT COMPLETED ===")
        
    except Exception as e:
        logger.error(f"Error in entrypoint: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    logger.info("=== STARTING APPLICATION ===")
    logger.info(f"LIVEKIT_URL: {os.environ.get('LIVEKIT_URL', 'NOT SET')}")
    logger.info(f"LIVEKIT_API_KEY: {'SET' if os.environ.get('LIVEKIT_API_KEY') else 'NOT SET'}")
    logger.info(f"LIVEKIT_API_SECRET: {'SET' if os.environ.get('LIVEKIT_API_SECRET') else 'NOT SET'}")
    
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="groq-enhanced-agent",  # Changed to match frontend expectation
        )
    )
