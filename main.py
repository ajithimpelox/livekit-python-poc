import logging
import sys
import os
from livekit.agents import JobContext, WorkerOptions, cli

# Set up logging first
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    from utils.common import prewarm, logger
    from agent import agent_entrypoint
    logger.info("All imports successful")
except ImportError as e:
    logger.error(f"Import error: {e}")
    sys.exit(1)

async def entrypoint(ctx: JobContext):
    """Router entrypoint that decides which agent to run."""
    try:
        room_name = ctx.job.room.name
        logger.info(f"=== ENTRYPOINT CALLED === Starting agent for room: {room_name}")
        
        await agent_entrypoint(ctx)
        
    except Exception as e:
        logger.error(f"Error in entrypoint: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    logger.info("🚀 STARTING APPLICATION ...")
    ws_url = os.environ.get("LIVEKIT_URL")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            ws_url=ws_url,
            http_proxy=None,
            agent_name="groq-enhanced-agent",
        )
    )
