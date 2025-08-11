import asyncio
import json
import traceback
from typing import Optional
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    AutoSubscribe,
    BackgroundAudioPlayer,
    AudioConfig,
    BuiltinAudioClip,
    MetricsCollectedEvent,
    metrics,
)
from livekit import rtc
from livekit.plugins import groq
from utils.common import logger, send_text_message, search_web
from database.db_queries import calculate_credits_used, check_customer_credits, deduct_customer_credits, get_chat_bot_by_id, get_agent_custom_prompt, get_lead_form, get_realtime_information, is_lead_already_exists, log_chat_transaction, create_user_lead_form
from utils.enums import ChatType
from tools.rag_tools import get_rag_information_from_vector_store
from utils.constants import PROMPTS
from mcp_client.agent_tools import MCPToolsIntegration
from mcp_client.util import MCPUtil
from mcp_client.server import MCPServerHttp
from datetime import datetime
from livekit.agents import llm


class UnifiedAgent(Agent):
    def __init__(self, prompt: str, agent_context: dict = None):
        super().__init__(
            instructions=prompt,
            # instructions="""You are a voice and text assistant. Be helpful and friendly.
            # You can chain multiple tools in one request to complete end-to-end tasks.
            # - Break down requests into steps, pick tools by name/description, and pass outputs between steps.
            # - Ask for any missing inputs before executing.
            # - Keep responses concise and actionable.
            # - Always confirm success or provide a clear error.
            # """,

            tools=[search_web],
        )
        self.mode = "voice"
        self.agent_context = agent_context or {}

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage):
        print(f"User turn completed: {new_message}")
        
        # Access variables from agent_entrypoint through context
        is_lead_form_active = self.agent_context.get('is_lead_form_active', False)
        handle_lead_form_response = self.agent_context.get('handle_lead_form_response')
        logger.info(f"on_user_turn_completed: {is_lead_form_active} {handle_lead_form_response}")
        if is_lead_form_active and handle_lead_form_response:
            await handle_lead_form_response(new_message.content)
            return


    async def handle_tool_error(self, error: Exception, context: dict) -> str:
        """Handle tool execution errors gracefully"""
        logger.error(f"Tool error: {error}, Context: {context}")
        if "unhashable type: 'list'" in str(error):
            return "There was an issue with the Gmail tool configuration. Please try a different approach."
        return f"Sorry, there was an error: {str(error)}"


async def agent_entrypoint(ctx: JobContext):
    logger.info(f"Agent entrypoint started")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(f"Connected to room for agent")

    await ctx.wait_for_participant()
    logger.info(f"Participant joined room")

    metadata = json.loads(ctx.room.metadata or "{}")
    logger.info(f"Metadata: {metadata}")

    conversation_id = metadata.get("conversationId") or 1
    customer_id = int(metadata.get("customerId") or 1)
    user_session_id = metadata.get("userSessionId") or 0
    knowledgebase_id = int(metadata.get("knowledgebaseId") or 1)
    is_embed_shared_chatbot = bool(metadata.get("isEmbedSharedChatbot")) or False
    logger.info(f"Conversation ID: {conversation_id}, Customer ID: {customer_id}, User Session ID: {user_session_id}, Knowledgebase ID: {knowledgebase_id}, Is embed shared chatbot: {is_embed_shared_chatbot}")

    existing_chatbot = await get_chat_bot_by_id(knowledgebase_id)
    logger.info(f"Existing chatbot: {existing_chatbot}")

    if not existing_chatbot:
        error_msg = f"Chatbot not found for id: {knowledgebase_id}"
        logger.error(f"Exception {error_msg} {traceback.format_exc()}")
        raise Exception(error_msg)

    try:
        await send_text_message(ctx, "message", "Checking credits...")

        # Assume check_customer_credits is an async function you have available
        credit_check = await check_customer_credits(
            customer_id, 20
        )  # Minimum 20 credits required
        logger.info(f"Credit check: {credit_check}")

        if not credit_check.get("has_credits"):
            await send_text_message(ctx, "loading", "", {"loading": False})
            await send_text_message(
                ctx, "credit-error", "Insufficient credits to start conversation"
            )
            # Disconnect the room and return early after 3.5 seconds
            await asyncio.sleep(3.5)
            if getattr(ctx, "room", None):
                await ctx.room.disconnect()
            return

        await send_text_message(ctx, "message", "Initilizing conversation...")

        logger.info(
            f"Credit check passed, Credits available: {credit_check.get('current_credits')}, Customer ID: {customer_id}"
        )
    except Exception as error:
        error_msg = "Failed to check customer credits"
        logger.error(f"{error_msg}, Customer ID: {customer_id}")

        await send_text_message(ctx, "loading", "Initilizing...", {"loading": False})
        await send_text_message(
            ctx,
            "credit-error",
            "Unable to verify credits. Please try again.",
            {"error": "CREDIT_CHECK_FAILED"},
        )
        await ctx.room.disconnect()
        return

    # Lead form state management
    is_lead_form_active = False
    current_lead_form_field_index = 0
    lead_form_responses = []
    lead_form_fields = []
    chat_bot_lead_form_id = 0
    # Initialize lead form if it exists and has fields
    if is_embed_shared_chatbot:
        lead_form = await get_lead_form(knowledgebase_id)
        logger.info(f"Lead form: {lead_form}")
        if len(lead_form.chatBotLeadInputField) > 0:
            is_lead_already_exists_result = await is_lead_already_exists(knowledgebase_id, lead_form.id, user_session_id, conversation_id)
            logger.info(f"Is lead already exists: {is_lead_already_exists_result}")
            if not is_lead_already_exists_result:
                is_lead_form_active = True
                lead_form_fields = lead_form.chatBotLeadInputField
                lead_form_responses = []
                current_lead_form_field_index = 0
                chat_bot_lead_form_id = lead_form.id

    namespace = existing_chatbot.get("namespace")
    index_name = existing_chatbot.get("index_name")
    print(f"Namespace: {namespace}, Index name: {index_name}")
    
    # Get custom prompt and knowledge base summary
    custom_prompt = await get_agent_custom_prompt(knowledgebase_id)
    kb_summary = ''
    
    # Get knowledge base summary from vector store
    try:
        kb_summary_result = await get_rag_information_from_vector_store(
            namespace, 
            index_name, 
            'Summarize the entire document for knowledge base', 
            20
        )
        
        if kb_summary_result and kb_summary_result.get('results'):
            kb_summary = '\n'.join([
                result[0].page_content if result and len(result) > 0 else ''
                for result in kb_summary_result['results']
            ])
    except Exception as e:
        logger.warning(f"Failed to get knowledge base summary: {e}")
    
    # Build final prompt with custom instructions and knowledge base context
    final_prompt = PROMPTS.get('realtimePrompt').replace('{KBSummary}', kb_summary or '').replace('{customMasterInstructions}', custom_prompt or '').replace('{currentDate}', datetime.now().isoformat())
    
    print(f"Final prompt: {final_prompt}")
    
    async def handle_lead_form_response(user_response: str) -> bool:
        """Helper function to handle lead form responses"""
        nonlocal is_lead_form_active, current_lead_form_field_index, lead_form_responses, lead_form_fields, chat_bot_lead_form_id
        logger.info(f"handle_lead_form_response: {is_lead_form_active}, {current_lead_form_field_index}, {lead_form_responses}, {lead_form_fields}, {chat_bot_lead_form_id}")
        if not is_lead_form_active or current_lead_form_field_index >= len(lead_form_fields):
            return False

        current_field = lead_form_fields[current_lead_form_field_index]
        logger.info(f"Processed question: {current_field}")
        
        lead_form_responses.append({
            "lable": current_field["label"],
            "value": user_response
        })

        current_lead_form_field_index += 1

        if current_lead_form_field_index < len(lead_form_fields):
            next_field = lead_form_fields[current_lead_form_field_index]
            question = next_field["placeholder"].replace("Insert ", "Please enter ", 1)
            logger.info(f"Processing next question: {question}")
            
            await session.say(question)
            return True
        else:
            # All fields completed, create the lead form and send confirmation
            try:                
                user_lead_dto = {
                    "conversationId": conversation_id,
                    "chatBotLeadFormId": chat_bot_lead_form_id,
                    "userSessionId": user_session_id or 0,
                    "form": lead_form_responses
                }

                success = await create_user_lead_form(knowledgebase_id, user_lead_dto)
                if success:
                    await session.say("Thanks for filling the form, you can now proceed with the conversation")
                    
                    # Reset lead form state
                    is_lead_form_active = False
                    current_lead_form_field_index = 0
                    lead_form_responses = []
                    lead_form_fields = []
                else:
                    await session.say("I apologize, but there was an issue saving your information. Let's continue with our conversation.")
                    
                    # Reset lead form state even on failure
                    is_lead_form_active = False
                    current_lead_form_field_index = 0
                    lead_form_responses = []
                    lead_form_fields = []
            except Exception as error:
                logger.error('Error creating user lead form', extra={
                    "error": str(error),
                    "customer_id": customer_id,
                    "conversation_id": conversation_id
                })

                await session.say("I apologize, but there was an issue saving your information. Let's continue with our conversation.")
                
                # Reset lead form state on error
                is_lead_form_active = False
                current_lead_form_field_index = 0
                lead_form_responses = []
                lead_form_fields = []
            
            return True

    agent = UnifiedAgent(final_prompt, agent_context={
        'is_lead_form_active': is_lead_form_active,
        'handle_lead_form_response': handle_lead_form_response
    })

    session = AgentSession(
        llm=groq.LLM(model=metadata.get("llmName") or "openai/gpt-oss-20b", temperature=0.8),
        # tts=groq.TTS(voice=metadata.get("voice") or "Cheyenne-PlayAI"),
        tts=groq.TTS(voice="Cheyenne-PlayAI"),
        stt=groq.STT(),
        vad=ctx.proc.userdata.get("vad"),
        max_tool_steps=8,
        allow_interruptions=True,
    )

    # Register MCP tools with the agent stays below, but we want background audio as early as possible.
    # Create the session first so background audio can bind to it for thinking cues.

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
                "url": "https://mcp.composio.dev/composio/server/34157b53-db3d-4f6b-89d6-9f2f7762ee84?transport=mcp&connected_account_id=dd1ca81c-a9f3-4240-a6a5-e115e0994424&user_id=gmail-1492",
                "timeout": 10,
            },
            name="composio_server",
        ),
        MCPServerHttp(
            params={
                "url": "https://mcp.composio.dev/composio/server/5efa70d8-e565-4566-885b-49cbb5181bca?transport=mcp&connected_account_id=543c29cf-068d-4082-8114-ef674132d00c&user_id=googledocs-1492",
                "timeout": 10,
            },
            name="composio_server",
        ),
    ]

    # Register MCP tools with the agent
    await MCPToolsIntegration.register_with_agent(agent, mcp_servers)

    # Inject dynamic tool schemas into instructions (generic, not tool-specific wording)
    try:
        schema_lines: list[str] = []
        for server in mcp_servers:
            try:
                function_tools = await MCPUtil.get_function_tools(
                    server, convert_schemas_to_strict=True
                )
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
                schema_lines.append(
                    f"- {ft.name}: required=[{required_str}] optional=[{optional_str}]"
                )

        if schema_lines:
            schemas_text = "\n".join(
                ["\nTool schemas (auto-generated):", *schema_lines]
            )
            # await agent.update_instructions(f"{agent.instructions}\n{schemas_text}")
    except Exception as e:
        logger.warning(f"Skipping dynamic schema injection: {e}")

    
    def on_lk_chat(text_reader, participant_identity):
        print(f"Received lk.chat event from {participant_identity}")
        asyncio.create_task(handle_lk_chat(text_reader))

    async def handle_lk_chat(text_reader):
        text = await text_reader.read_all()
        print(f"Received text message: {text}")

        credit_check = await check_customer_credits(customer_id, 10)  # Need at least 10 credits to continue
        logger.info(f"handle_lk_chat Credit check: {credit_check}")
        if not credit_check.get("has_credits"):
            await send_text_message(ctx.room, "loading", "", {"loading": False})
            await send_text_message(ctx.room, "credit-error", "Insufficient credits to continue conversation")

            logger.warning(
                f"Speech input blocked due to insufficient credits. Current: {credit_check.get('current_credits')}, Required: 10, Customer ID: {customer_id}"
            )
            return  # Don't process the speech

        log_chat_transaction({
          "conversationId": conversation_id,
          "customerId": customer_id,
          "userSessionId": user_session_id,
          "message": text,
          "isQuestion": True,
          "chatType": ChatType.normal,
          "credits": 1
        })

        await process_text_message(ctx.room, session, text)

    async def process_text_message(
      room: rtc.Room,
      session: AgentSession,
      message: str,
      background_audio: Optional[BackgroundAudioPlayer] = None,
    ):
        try:
            logger.info(f"Processing text message: {message}")
            await send_text_message(room, "message", "Processing your message...")
    
            # Background audio will automatically play during processing because we set it up
            # in the agent_entrypoint function with the BackgroundAudioPlayer
    
            # Proactively play a short typing cue to ensure audible feedback during processing
            try:
                if background_audio is not None:
                    background_audio.play(BuiltinAudioClip.KEYBOARD_TYPING)
            except Exception as e:
                logger.warning(f"Failed to play typing cue: {e}")

            if getattr(room, "local_participant", None):
                logger.info(f"Sending text message: {message}")
                await room.local_participant.send_text(message, topic="lk.chat")

            if is_lead_form_active:
                logger.info(f"Processing lead form response: {message}")
                await handle_lead_form_response(message)
                return
            else:
              # Configure the agent to use appropriate tools based on the message
              tool_choice = "auto"
      
              speech_handle = session.generate_reply(
                  user_input=message, tool_choice=tool_choice
              )
      
              # Send text response
              async def _send_text():
                text = getattr(speech_handle, "text", None)
                if text:
                    await send_text_message(room, "message", text)
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
    
                        await send_text_message(room, "message", formatted_text)
      
              text_task = asyncio.create_task(_send_text())
      
              await speech_handle
              await text_task  # Ensure text streaming is complete
      
            logger.info("Text message processed, voice response sent")
        except Exception as e:
          logger.error(f"Error processing text message: {e}")
          error_msg = (
              "I encountered an error while processing your message. Please try again."
          )
          await session.say(error_msg, allow_interruptions=False)
          await send_text_message(room, "message", error_msg)

    ctx.room.register_text_stream_handler("lk.chat", on_lk_chat)

    await session.start(agent=agent, room=ctx.room)
    logger.info("Agent session started")
    
    # Get realtime information before using it in greeting
    
    greeting = f"Provide a warm, friendly greeting to the user. Keep it brief and welcoming. Make it different each time."

    if is_embed_shared_chatbot:
        if is_lead_form_active:
                await send_text_message(ctx, "message", "Starting lead form...")
                first_field = lead_form_fields[current_lead_form_field_index]
                question = first_field["placeholder"].replace("Insert ", "Please enter ", 1)
                logger.info(f"Initial lead form question: {question}")
                await session.say(question)
        else:
           await send_text_message(ctx.room, "message", "Generating greeting...")
           await session.generate_reply(instructions=greeting)
    else:
        realtime_information = await get_realtime_information(customer_id)
        print(f"Realtime information: {realtime_information}")
        # greeting = f"Hello! I'm your Groq assistant. How can I help you today?"
        # await session.say(greeting, allow_interruptions=True)

        await session.generate_reply(instructions=greeting) 
        if len(realtime_information) > 0:
            greeting_instructions = f"These are the custom relevant information here: {json.dumps(realtime_information)}" + \
                  'Provide a warm greeting to the user. Use the data present in the memory to construct the greeting. Add some flavor text using the information provided. If it is empty provide a generic greeting. Reply with audio always. Make it different each time'
            await session.generate_reply(instructions=greeting_instructions)
        else:
            await session.generate_reply(instructions=greeting)

    @session.on('user_input_transcribed')
    def on_user_input_transcribed(user_input: str):
        print(f"User input transcribed: {user_input}")
        asyncio.create_task(handle_user_input_transcribed(user_input))  

    async def handle_user_input_transcribed(user_input: str):
        try:
            credit_check = await check_customer_credits(customer_id, 10)  # Need at least 10 credits to continue

            if not credit_check.get("has_credits"):
                await send_text_message(ctx.room, "loading", "", {"loading": False})
                await send_text_message(ctx.room, "credit-error", "Insufficient credits to continue conversation")

                logger.warning(
                    f"Speech input blocked due to insufficient credits. Current: {credit_check.get('current_credits')}, Required: 10, Customer ID: {customer_id}"
                )
                return  # Don't process the speech
        except Exception as error:
            logger.error(
                f"Error checking credits during speech processing. Customer ID: {customer_id}, Error: {str(error)}"
            )
            return  # Don't process speech on credit check error
        
        log_chat_transaction({
          "conversationId": conversation_id,
          "customerId": customer_id,
          "userSessionId": user_session_id,
          "message": user_input,
          "isQuestion": True,
          "chatType": ChatType.normal,
          "credits": 1
        })

    @session.on('metrics_collected')
    def on_metrics_collected(metric: MetricsCollectedEvent):
        if isinstance(metric.metrics, metrics.LLMMetrics):
          print(f"Metrics collected: {metrics}")
          total_tokens = metric.metrics.total_tokens
          logger.info(f"Total tokens: {total_tokens}")
          asyncio.create_task(deduct_credits(customer_id, total_tokens))
    
    async def deduct_credits(customer_id: int, total_tokens: int):
        totalCredits = await calculate_credits_used(total_tokens)
        logger.info(f"Deducting credits: {totalCredits} for customer: {customer_id}")
        await deduct_customer_credits(customer_id, totalCredits)

    logger.info(f"Greeting sent for agent")
