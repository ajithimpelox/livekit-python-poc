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
    metrics,
)
from livekit.agents.llm import (
    ChatContext,
    ChatMessage,
)
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import silero, groq
from livekit import api, rtc

from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


class EnhancedVoiceTextAgent:
    """Enhanced agent that handles both voice and text interactions"""
    
    def __init__(self, ctx: JobContext, text_only: bool = False):
        self.ctx = ctx
        self.room = ctx.room
        self.conversation_history = []
        self.is_voice_active = not text_only
        self.text_only = text_only
        
        # Initialize chat context
        if text_only:
            system_content = "You are the Groq text assistant with voice output. You interact with users through text messages and respond with both text and spoken audio. Be helpful, friendly, and provide clear, informative responses."
        else:
            system_content = "You are the Groq voice and text assistant. You can interact with users through both voice and text messages. Be helpful, friendly, and adapt your responses appropriately for the medium being used. When responding to text, be concise but informative. When responding to voice, be conversational and natural."
        
        self.chat_ctx = ChatContext(
            messages=[
                ChatMessage(
                    role="system",
                    content=system_content,
                )
            ]
        )
        
        # Always create voice pipeline agent for TTS capabilities
        # The difference is whether we start its listening capabilities or not
        self.voice_agent = VoicePipelineAgent(
            vad=ctx.proc.userdata["vad"],
            stt=groq.STT(),
            llm=groq.LLM(),
            tts=groq.TTS(voice="Cheyenne-PlayAI"),
            chat_ctx=self.chat_ctx,
        )
        
        # Setup metrics collection
        @self.voice_agent.on("metrics_collected")
        def _on_metrics_collected(mtrcs: metrics.AgentMetrics):
            metrics.log_metrics(mtrcs)
        
        if text_only:
            logger.info("Text-only mode: Voice agent created for TTS only, no microphone listening will be started")
        else:
            logger.info("Voice mode: Full voice pipeline initialized")
    
    async def speak_text(self, text: str):
        """Speak text using TTS in text-only mode"""
        try:
            if self.text_only and self.voice_agent:
                logger.info(f"Speaking text in text-only mode: {text[:50]}...")
                
                # Use the voice agent's say method to speak the exact text
                # Since we don't start the voice agent's listening in text mode,
                # this will only do TTS without STT interference
                await self.voice_agent.say(text, allow_interruptions=False)
                
                logger.info("Text-to-speech audio sent successfully")
                
        except Exception as e:
            logger.error(f"Error in speak_text: {e}")
    
    async def send_data_to_frontend(self, topic: str, message: str, additional_data: Optional[Dict[str, Any]] = None):
        """Send data to frontend similar to Node.js implementation"""
        try:
            data = {
                "type": "chat_response",  # Standardize the type
                "message": message,
                "timestamp": int(datetime.now().timestamp() * 1000),
                **(additional_data or {})
            }
            
            data_str = json.dumps(data)
            data_bytes = data_str.encode('utf-8')
            
            await self.room.local_participant.publish_data(
                payload=data_bytes,
                reliable=True,
                topic="lk.chat.response"  # Always use the correct topic
            )
            logger.info(f"Sent data to frontend - Topic: lk.chat.response, Message: {message}, Data size: {len(data_bytes)} bytes")
        except Exception as e:
            logger.error(f"Failed to send data to frontend: {e}")
    
    async def log_chat_transaction(self, message: str, is_question: bool = True):
        """Log chat transaction (placeholder for database logging)"""
        try:
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "message": message,
                "is_question": is_question,
                "chat_type": "normal"
            }
            self.conversation_history.append(log_entry)
            logger.info(f"Chat logged: {message[:50]}...")
        except Exception as e:
            logger.error(f"Failed to log chat transaction: {e}")
    
    async def process_text_message(self, message: str, participant_info):
        """Process incoming text messages"""
        try:
            logger.info(f"Processing text message: {message}")
            
            # Log the incoming message
            await self.log_chat_transaction(message, is_question=True)
            
            # Add user message to chat context
            self.chat_ctx.messages.append(
                ChatMessage(role="user", content=message)
            )
            
            # Send loading indicator
            await self.send_data_to_frontend("lk.chat.response", "Processing your message...")
            
            # Get LLM response for text
            llm_response = await self._get_llm_response_for_text(message)
            
            # Log the response
            await self.log_chat_transaction(llm_response, is_question=False)
            
            # Send response back to user via text
            response_data = {
                "type": "chat_response",
                "message": llm_response,
                "timestamp": int(datetime.now().timestamp() * 1000)
            }
            payload_str = json.dumps(response_data)
            payload_bytes = payload_str.encode('utf-8')
            
            logger.info(f"Sending text response - Topic: lk.chat.response, Data: {payload_str[:100]}...")
            
            await self.room.local_participant.publish_data(
                payload=payload_bytes,
                reliable=True,
                topic="lk.chat.response"
            )
            
            logger.info(f"Text response sent successfully - Size: {len(payload_bytes)} bytes")
            
            # Also update the chat context with assistant response
            self.chat_ctx.messages.append(
                ChatMessage(role="assistant", content=llm_response)
            )

            # Generate audio response based on mode
            if self.text_only:
                # Text-only mode: use direct TTS without microphone interference
                try:
                    logger.info("Sending voice response for text message (text-only mode)")
                    await self.speak_text(llm_response)
                except Exception as e:
                    logger.error(f"Error sending voice response in text-only mode: {e}")
            elif self.voice_agent:
                # Voice mode: use voice agent's say method (but this shouldn't happen for text messages)
                try:
                    logger.info("Sending voice response for text message (voice mode)")
                    await self.voice_agent.say(llm_response, allow_interruptions=False)
                    logger.info("Voice response for text message sent")
                except Exception as e:
                    logger.error(f"Error sending voice response: {e}")
            
            logger.info("Text message processed successfully")
            
        except Exception as e:
            logger.error(f"Error processing text message: {e}")
            await self.send_data_to_frontend("lk.chat.response", f"Failed to process message: {str(e)}")
    
    async def _get_llm_response_for_text(self, message: str) -> str:
        """Get LLM response specifically for text messages"""
        try:
            # Create a temporary chat context with the current message
            temp_messages = self.chat_ctx.messages.copy()
            temp_messages.append(ChatMessage(role="user", content=message))
            
            # Use Groq LLM to generate response
            llm = groq.LLM()
            temp_ctx = ChatContext(messages=temp_messages)
            
            # Generate response
            llm_stream = llm.chat(chat_ctx=temp_ctx)
            response_content = ""
            
            async for chunk in llm_stream:
                if chunk.choices and len(chunk.choices) > 0:
                    if chunk.choices[0].delta and chunk.choices[0].delta.content:
                        response_content += chunk.choices[0].delta.content
            
            return response_content.strip() if response_content else "I apologize, but I couldn't generate a response. Please try again."
            
        except Exception as e:
            logger.error(f"Error getting LLM response: {e}")
            return f"I apologize, but I encountered an error while processing your message: {str(e)}"
    
    async def setup_text_handler(self):
        """Setup text message handler using LiveKit's data_received event"""
        try:
            # Register data channel handler for text messages
            def on_data_received(data: rtc.DataPacket):
                try:
                    if data.topic == "lk.chat":
                        message = data.data.decode('utf-8')
                        participant_identity = data.participant.identity if data.participant else "unknown"
                        
                        logger.info(f"Received text message from {participant_identity}: {message}")
                        
                        # Process the text message using asyncio.create_task
                        asyncio.create_task(self.process_text_message(message, {
                            "identity": participant_identity,
                            "participant": data.participant
                        }))
                        
                except Exception as e:
                    logger.error(f"Error handling data received: {e}")
            
            self.room.on("data_received", on_data_received)
            logger.info("Text message handler setup complete")
            
        except Exception as e:
            logger.error(f"Failed to setup text handler: {e}")
    
    async def send_initial_messages(self):
        """Send initial greeting messages after participant joins"""
        try:
            # Send initial greeting
            await self.send_data_to_frontend("lk.chat.response", "Initializing assistant...")
            await asyncio.sleep(1)  # Give time for setup
            
            if self.text_only:
                # Text-only mode: send text greeting and speak it using TTS
                greeting_text = "Hello! I'm your Groq text assistant. Send me a text message and I'll respond with both text and voice. How can I help you today?"
                
                # Send text greeting
                await self.send_data_to_frontend("lk.chat.response", greeting_text)
                
                # Speak the greeting using voice agent (but without starting listening)
                await self.speak_text(greeting_text)
                
            else:
                # Voice mode: send both voice and text greetings
                await self.voice_agent.say(
                    "Hello! I'm your Groq assistant. You can talk to me using voice or send me text messages. How can I help you today?", 
                    allow_interruptions=True
                )
                
                # Also send text greeting
                await self.send_data_to_frontend("lk.chat.response", "Assistant ready! You can use voice or text to chat with me.")
            
            logger.info("Initial messages sent successfully")
            
        except Exception as e:
            logger.error(f"Failed to send initial messages: {e}")
            await self.send_data_to_frontend("lk.chat.response", f"Failed to send greeting: {str(e)}")
    
    async def start(self):
        """Start the enhanced agent with both voice and text capabilities (deprecated - use individual methods)"""
        try:
            # Setup text message handling
            await self.setup_text_handler()
            
            # Start the voice agent
            self.voice_agent.start(self.room)
            
            # Send initial messages
            await self.send_initial_messages()
            
            logger.info("Enhanced voice and text agent started successfully")
            
        except Exception as e:
            logger.error(f"Failed to start enhanced agent: {e}")
            await self.send_data_to_frontend("error", f"Failed to initialize: {str(e)}")


async def entrypoint(ctx: JobContext):
    """Main entrypoint that supports both voice and text interactions"""
    try:
        # Detect connection mode based on room name
        room_name = ctx.job.room.name
        is_text_mode = room_name.startswith("text_")
        
        logger.info(f"Connecting to room... (UI Mode: {'text' if is_text_mode else 'voice'})")
        
        # Connect with appropriate settings based on mode
        # Text mode still needs audio publishing capabilities for TTS
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        
        logger.info("Connected to room successfully")
        
        # Create the enhanced agent with appropriate mode
        enhanced_agent = EnhancedVoiceTextAgent(ctx, text_only=is_text_mode)
        
        # Setup text handler for both modes - this handles text chat
        await enhanced_agent.setup_text_handler()
        logger.info("Text stream handler setup complete")
        
        # Start voice agent with different configurations based on mode
        if enhanced_agent.voice_agent:
            if is_text_mode:
                # For text mode, we need to start the voice agent for TTS but disable STT
                enhanced_agent.voice_agent.start(ctx.room)
                # Try to disable the voice agent's listening capabilities
                try:
                    # Disable VAD to prevent microphone listening
                    if hasattr(enhanced_agent.voice_agent, '_pipeline') and hasattr(enhanced_agent.voice_agent._pipeline, '_vad'):
                        enhanced_agent.voice_agent._pipeline._vad = None
                        logger.info("VAD disabled for text-only mode")
                except Exception as e:
                    logger.warning(f"Could not disable VAD: {e}")
                
                logger.info("Voice agent started for TTS only (text mode)")
            else:
                # Voice mode: full voice pipeline
                enhanced_agent.voice_agent.start(ctx.room)
                logger.info("Voice agent started (full voice pipeline)")
        else:
            logger.info("Voice agent not available")
        
        # Wait for participant to join
        logger.info("Waiting for participant to join...")
        await ctx.wait_for_participant()
        logger.info("Participant connected, sending initial messages...")
        
        # Send initial messages after participant joins
        await enhanced_agent.send_initial_messages()
        
        # Keep the connection alive and handle text messages
        while True:
            await asyncio.sleep(1)
            
    except Exception as e:
        logger.error(f"Error in entrypoint: {e}")
        raise


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="groq-enhanced-agent",
        )
    )
