import asyncio
from typing import Optional
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    AutoSubscribe,
    BackgroundAudioPlayer,
    AudioConfig,
    BuiltinAudioClip
)
from livekit import rtc
from common import logger, send_text_message, search_web
from mcp_client.agent_tools import MCPToolsIntegration
from mcp_client.util import MCPUtil
from mcp_client.server import MCPServerHttp


class UnifiedAgent(Agent):
    def __init__(self, mode="voice"):
        super().__init__(
            instructions="""You are a voice and text assistant. Be helpful and friendly.
            You can chain multiple tools in one request to complete end-to-end tasks.
            - Break down requests into steps, pick tools by name/description, and pass outputs between steps.
            - Ask for any missing inputs before executing.
            - Keep responses concise and actionable.
            - Always confirm success or provide a clear error.
            """,
            tools=[search_web],
        )
        self.mode = mode

    async def handle_tool_error(self, error: Exception, context: dict) -> str:
        """Handle tool execution errors gracefully"""
        logger.error(f"Tool error: {error}, Context: {context}")
        if "unhashable type: 'list'" in str(error):
            return "There was an issue with the Gmail tool configuration. Please try a different approach."
        return f"Sorry, there was an error: {str(error)}"


async def agent_entrypoint(ctx: JobContext, mode: str):
    logger.info(f"{mode.capitalize()} agent entrypoint started")

    if mode == "voice":
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    else:
        await ctx.connect(auto_subscribe=False)

    logger.info(f"Connected to room for {mode} agent")

    agent = UnifiedAgent(mode=mode)

    # Register MCP tools with the agent stays below, but we want background audio as early as possible.
    # Create the session first so background audio can bind to it for thinking cues.
    session = AgentSession(
        llm=ctx.llm,
        tts=ctx.tts,
        stt=ctx.stt if mode == "voice" else None,
        vad=ctx.proc.userdata.get("vad") if mode == "voice" else None,
        max_tool_steps=8,
        allow_interruptions=True if mode == "voice" else False,
    )

    # Start background audio immediately after connection, bound to this session
    background_audio = BackgroundAudioPlayer(
        ambient_sound=AudioConfig(
            BuiltinAudioClip.OFFICE_AMBIENCE, volume=1.35),
        thinking_sound=[
            AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=1.6),
        ],
    )
    await background_audio.start(room=ctx.room, agent_session=session)
    # Save for later use inside message processing
    ctx.proc.userdata["background_audio"] = background_audio

    # Setup MCP servers
    mcp_servers = [
        MCPServerHttp(
            params={
                "url": 'https://mcp.composio.dev/composio/server/34157b53-db3d-4f6b-89d6-9f2f7762ee84?transport=mcp&connected_account_id=dd1ca81c-a9f3-4240-a6a5-e115e0994424&user_id=gmail-1492',
                "timeout": 10,
            },
            name="composio_server"
        ),
         MCPServerHttp(
            params={
                "url": 'https://mcp.composio.dev/composio/server/5efa70d8-e565-4566-885b-49cbb5181bca?transport=mcp&connected_account_id=543c29cf-068d-4082-8114-ef674132d00c&user_id=googledocs-1492',
                "timeout": 10,
            },
            name="composio_server"
        )
    ]

    # Register MCP tools with the agent
    await MCPToolsIntegration.register_with_agent(agent, mcp_servers)

    # Inject dynamic tool schemas into instructions (generic, not tool-specific wording)
    try:
        schema_lines: list[str] = []
        for server in mcp_servers:
            try:
                function_tools = await MCPUtil.get_function_tools(server, convert_schemas_to_strict=True)
            except Exception as e:
                logger.warning(f"Failed to fetch tool schemas from {server.name}: {e}")
                continue

            for ft in function_tools:
                props = (ft.params_json_schema or {}).get("properties", {})
                required = list((ft.params_json_schema or {}).get("required", []))
                all_params = list(props.keys())
                optional = [p for p in all_params if p not in required]
                # Keep line concise
                required_str = ", ".join(required) if required else "-"
                optional_str = ", ".join(optional) if optional else "-"
                schema_lines.append(f"- {ft.name}: required=[{required_str}] optional=[{optional_str}]")

        if schema_lines:
            schemas_text = "\n".join(["\nTool schemas (auto-generated):", *schema_lines])
            await agent.update_instructions(f"{agent.instructions}\n{schemas_text}")
    except Exception as e:
        logger.warning(f"Skipping dynamic schema injection: {e}")

    if mode == "text":
        def on_data_received(data: rtc.DataPacket):
            if data.topic == "lk.chat":
                message = data.data.decode('utf-8')
                logger.info(f"Received text message: {message}")
                asyncio.create_task(process_text_message(
                    ctx.room, session, message, ctx.proc.userdata.get("background_audio")
                ))
        ctx.room.on("data_received", on_data_received)
        logger.info("Text message handler set up")

    await session.start(agent=agent, room=ctx.room)
    logger.info("Agent session started")

    await ctx.wait_for_participant()
    logger.info(f"Participant joined {mode} room")

    # Send greeting while ambient background audio is already playing
    greeting = f"Hello! I'm your Groq {mode} assistant. How can I help you today?"
    await session.say(greeting, allow_interruptions=(mode == "voice"))
    await send_text_message(ctx.room, greeting)
    logger.info(f"Greeting sent for {mode} agent")


async def process_text_message(
    room: rtc.Room,
    session: AgentSession,
    message: str,
    background_audio: Optional[BackgroundAudioPlayer] = None,
):
    try:
        logger.info(f"Processing text message: {message}")
        await send_text_message(room, "Processing your message...")

        # Background audio will automatically play during processing because we set it up
        # in the agent_entrypoint function with the BackgroundAudioPlayer

        # Proactively play a short typing cue to ensure audible feedback during processing
        try:
            if background_audio is not None:
                background_audio.play(BuiltinAudioClip.KEYBOARD_TYPING)
        except Exception as e:
            logger.warning(f"Failed to play typing cue: {e}")

        # Configure the agent to use appropriate tools based on the message
        tool_choice = "auto"

        speech_handle = session.generate_reply(
            user_input=message, tool_choice=tool_choice)

        # Send text response
        async def _send_text():
            text = getattr(speech_handle, "text", None)
            if text:
                await send_text_message(room, text)
            else:
                # If no direct text response, format the tool execution result
                result = getattr(speech_handle, "result", None)
                if result and hasattr(result, "content"):
                    try:
                        content = result.content[0].text
                        # Try to parse JSON if it's a tool result
                        if content.startswith("{"):
                            import json
                            data = json.loads(content)
                            if "data" in data and "successful" in data:
                                formatted_text = "Here's what I found:\n\n"
                                if "threads" in data["data"]:
                                    # Limit to 5 emails
                                    for thread in data["data"]["threads"][:5]:
                                        formatted_text += f"- {thread['snippet']}\n\n"
                                else:
                                    formatted_text = content
                            else:
                                formatted_text = content
                        else:
                            formatted_text = content
                    except Exception as e:
                        logger.error(f"Error formatting response: {e}")
                        formatted_text = content if content else "No content available"

                    await send_text_message(room, formatted_text)

        text_task = asyncio.create_task(_send_text())

        await speech_handle
        await text_task  # Ensure text streaming is complete

        logger.info("Text message processed, voice response sent")
    except Exception as e:
        logger.error(f"Error processing text message: {e}")
        error_msg = "I encountered an error while processing your message. Please try again."
        await session.say(error_msg, allow_interruptions=False)
        await send_text_message(room, error_msg)
