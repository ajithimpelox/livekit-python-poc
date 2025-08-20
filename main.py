import logging
import sys
import os
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.plugins import groq, openai, google

# Set up logging first
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    from common import prewarm, logger as common_logger
    from agent import agent_entrypoint
    logger.info("All imports successful")
except ImportError as e:
    logger.error(f"Import error: {e}")
    sys.exit(1)

def get_llm_provider(provider_name: str, model: str, **kwargs):
    """Dynamic LLM provider selection based on job metadata."""
    providers = {
        "groq": lambda: groq.LLM(model=model, **kwargs),
        "openai": lambda: openai.LLM(model=model, **kwargs),
        "google": lambda: google.LLM(model=model, **kwargs),
        "gemini": lambda: google.LLM(model=model, **kwargs),  # Alias
    }
    # Default to Groq if provider not recognized
    return providers.get(provider_name.lower(), providers["groq"])()

def get_tts_provider(provider_name: str, metadata: dict | None = None):
    """Dynamic TTS provider selection based on job metadata."""
    metadata = metadata or {}
    provider = (provider_name or "groq").lower()

    if provider == "groq" or provider == "gemini":
        requested_voice = metadata.get("voice") or "Cheyenne-PlayAI"
        arabic_voices = {"Ahmad-PlayAI", "Amira-PlayAI", "Khalid-PlayAI", "Nasser-PlayAI"}
        tts_model = "playai-tts-arabic" if requested_voice in arabic_voices else "playai-tts"
        return groq.TTS(voice=requested_voice, model=tts_model)

    if provider in {"openai"}:
        # Use defaults; plugin will pick a sensible model/voice
        return openai.TTS()
    # Fallback
    return groq.TTS()

def get_stt_provider(provider_name: str):
    """Dynamic STT provider selection based on job metadata."""
    provider = (provider_name or "groq").lower()

    if provider == "groq":
        return groq.STT()
    if provider == "openai":
        return openai.STT()
    if provider in {"google", "gemini"}:
        return groq.STT()
    return groq.STT()

async def entrypoint(ctx: JobContext):
    """Router entrypoint that decides which agent to run."""
    try:
        room_name = ctx.job.room.name
        logger.info(f"=== ENTRYPOINT CALLED === Starting agent for room: {room_name}")
        logger.info(f"Job ID: {ctx.job.id}")
        logger.info(f"Room ID: {ctx.job.room.sid}")
        
        # Initialize plugins
        logger.info("Initializing plugins...")
        metadata = getattr(ctx.job, "metadata", None) or {}
        provider = metadata.get("provider", "gemini")
        model = metadata.get("llmName") or 'gemini-2.0-flash-exp'

        ctx.llm = get_llm_provider(
            provider_name=provider,
            model=model,
            temperature=0.5,
            tool_choice='auto',
        )

        ctx.tts = get_tts_provider(provider_name=provider, metadata=metadata)
        ctx.stt = get_stt_provider(provider_name=provider)
        # VAD is initialized in prewarm and stored in ctx.proc.userdata["vad"]
        
        logger.info("Plugins initialized successfully")

        if room_name.startswith("text_"):
            logger.info("Running in text mode")
            await agent_entrypoint(ctx, "text")
        else:
            logger.info("Running in voice mode")
            await agent_entrypoint(ctx, "voice")
            
        logger.info("=== ENTRYPOINT COMPLETED ===")
        
    except Exception as e:
        logger.error(f"Error in entrypoint: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    logger.info("=== STARTING APPLICATION ===")
    logger.info(f"LIVEKIT_URL: {os.environ.get('LIVEKIT_URL', 'NOT SET')}")
    logger.info(f"LIVEKIT_API_KEY: {'SET' if os.environ.get('LIVEKIT_API_KEY') else 'NOT SET'}")
    logger.info(f"LIVEKIT_API_SECRET: {'SET' if os.environ.get('LIVEKIT_API_SECRET') else 'NOT SET'}")
    
    ws_url = os.environ.get("LIVEKIT_URL")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="groq-enhanced-agent",
            ws_url=ws_url,
            http_proxy=None,
        )
    )
