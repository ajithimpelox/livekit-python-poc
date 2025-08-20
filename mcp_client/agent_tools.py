import asyncio
import logging
import json
import inspect
import typing
from typing import Any, List, Dict, Callable, Optional, Awaitable, Sequence, Tuple, Type, Union, cast
from uuid import uuid4

# Import from the MCP module
from .util import MCPUtil, FunctionTool
from .server import MCPServer
from livekit.agents import ChatContext, AgentSession, JobContext, FunctionTool as Tool
from mcp import CallToolRequest

logger = logging.getLogger("mcp-agent-tools")

class MCPToolsIntegration:
    """
    Helper class for integrating MCP tools with LiveKit agents.
    Provides utilities for registering dynamic tools from MCP servers.
    """

    @staticmethod
    async def prepare_dynamic_tools(mcp_servers: List[MCPServer],
                                   convert_schemas_to_strict: bool = True,
                                   auto_connect: bool = True) -> List[Callable]:
        """
        Fetches tools from multiple MCP servers and prepares them for use with LiveKit agents.

        Args:
            mcp_servers: List of MCPServer instances
            convert_schemas_to_strict: Whether to convert JSON schemas to strict format
            auto_connect: Whether to automatically connect to servers if they're not connected

        Returns:
            List of decorated tool functions ready to be added to a LiveKit agent
        """
        prepared_tools = []

        # Ensure all servers are connected if auto_connect is True
        if auto_connect:
            for server in mcp_servers:
                if not getattr(server, 'connected', False):
                    try:
                        logger.debug(f"Auto-connecting to MCP server: {server.name}")
                        await server.connect()
                    except Exception as e:
                        logger.error(f"Failed to connect to MCP server {server.name}: {e}")

        # Process each server
        for server in mcp_servers:
            logger.info(f"Fetching tools from MCP server: {server.name}")
            try:
                mcp_tools = await MCPUtil.get_function_tools(
                    server, convert_schemas_to_strict=convert_schemas_to_strict
                )
                logger.info(f"Received {len(mcp_tools)} tools from {server.name}")
            except Exception as e:
                logger.error(f"Failed to fetch tools from {server.name}: {e}")
                continue

            # Process each tool from this server (enforce a hard limit to satisfy upstream model caps)
            # Many MCP servers expose a large catalog; keep only the first N most relevant tools by default.
            MAX_TOOLS_PER_SERVER = 100
            for tool_instance in mcp_tools[:MAX_TOOLS_PER_SERVER]:
                # Some tools have strict param names that models frequently misspell.
                # To avoid validation failures before invocation, force a permissive fallback wrapper.
                # Decide fallback based on schema: if the tool accepts free-form text-like fields,
                # rely on the permissive wrapper to avoid strict arg-name validation issues.
                schema_keys = set((tool_instance.params_json_schema or {}).get("properties", {}).keys()) if hasattr(tool_instance, "params_json_schema") else set()
                fallback_trigger_keys = {"text", "markdown_text", "body"}
                force_fallback = bool(schema_keys & fallback_trigger_keys)

                if force_fallback:
                    try:
                        fallback_tool = MCPToolsIntegration._create_fallback_tool(tool_instance)
                        prepared_tools.append(fallback_tool)
                        logger.info(f"Registered fallback tool for '{tool_instance.name}' (schema-triggered)")
                        continue
                    except Exception as fe:
                        logger.error(f"Failed to register schema-triggered fallback for '{tool_instance.name}': {fe}")

                try:
                    decorated_tool = MCPToolsIntegration._create_decorated_tool(tool_instance)
                    prepared_tools.append(decorated_tool)
                    logger.debug(f"Successfully prepared tool: {tool_instance.name}")
                except Exception as e:
                    logger.error(f"Failed to prepare tool '{tool_instance.name}': {e}")
                    # Fallback registration with generic payload parameter
                    try:
                        fallback_tool = MCPToolsIntegration._create_fallback_tool(tool_instance)
                        prepared_tools.append(fallback_tool)
                        logger.warning(
                            f"Registered fallback tool for '{tool_instance.name}' with generic payload param"
                        )
                    except Exception as fe:
                        logger.error(
                            f"Failed to register fallback tool for '{tool_instance.name}': {fe}"
                        )

        return prepared_tools

    @staticmethod
    def _create_decorated_tool(tool: FunctionTool) -> Callable:
        """
        Creates a decorated function for a single MCP tool that can be used with LiveKit agents.

        Args:
            tool: The FunctionTool instance to convert

        Returns:
            A decorated async function that can be added to a LiveKit agent's tools
        """
        # Get function_tool decorator from LiveKit
        # Import locally to avoid circular imports
        from livekit.agents.llm import function_tool

        # Create parameters list from JSON schema
        params = []
        annotations = {}
        schema_props = tool.params_json_schema.get("properties", {})
        schema_required = set(tool.params_json_schema.get("required", []))
        type_map = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
        }

        # Build parameters from the schema properties
        for p_name, p_details in schema_props.items():
            json_type = p_details.get("type", "string")
            # Map arrays/objects to precise typing for schema emission
            if json_type == "array":
                item_schema = p_details.get("items") or {}
                item_json_type = item_schema.get("type", "string")
                item_py_type = type_map.get(item_json_type, str)
                py_type = typing.List[item_py_type]  # type: ignore[index]
            elif json_type == "object":
                py_type = typing.Dict[str, typing.Any]
            else:
                py_type = type_map.get(json_type, str)
            annotations[p_name] = py_type

            # Avoid unhashable defaults entirely by omitting them when problematic
            default = inspect.Parameter.empty
            if p_name not in schema_required:
                default_val = p_details.get("default")
                if default_val is None:
                    default = None
                else:
                    try:
                        hash(default_val)
                        default = default_val
                    except TypeError:
                        # Do not set a default if it's not hashable (lists/dicts/nested)
                        default = inspect.Parameter.empty

            params.append(inspect.Parameter(
                name=p_name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                annotation=py_type,
                default=default
            ))

        # Define the actual function that will be called by the agent
        async def tool_impl(**kwargs):
            input_json = json.dumps(kwargs)
            logger.info(f"Invoking tool '{tool.name}' with args: {kwargs}")
            result_str = await tool.on_invoke_tool(None, input_json)
            logger.info(f"Tool '{tool.name}' result: {result_str}")
            return result_str

        # Set function metadata
        tool_impl.__signature__ = inspect.Signature(parameters=params)
        tool_impl.__name__ = tool.name
        tool_impl.__doc__ = tool.description
        tool_impl.__annotations__ = {'return': str, **annotations}

        # Apply the decorator and return
        return function_tool()(tool_impl)

    @staticmethod
    def _create_fallback_tool(tool: FunctionTool) -> Callable:
        """
        Fallback registration when strict signature generation fails.
        Exposes keyword params based on the MCP tool schema (all optional, typed as Any),
        and forwards them as a JSON payload unchanged.
        """
        from livekit.agents.llm import function_tool

        # Build a permissive signature from the MCP tool schema
        schema_props = tool.params_json_schema.get("properties", {}) if hasattr(tool, "params_json_schema") else {}
        params: list[inspect.Parameter] = []

        # Map JSON schema types to Python types for better provider compatibility (OpenAI requires explicit types)
        type_map: dict[str, type] = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
        }

        for p_name, p_details in schema_props.items():
            json_type = p_details.get("type") or "string"
            if json_type == "array":
                item_schema = p_details.get("items") or {}
                item_json_type = item_schema.get("type", "string")
                item_py_type = type_map.get(item_json_type, str)
                py_type = typing.List[item_py_type]  # type: ignore[index]
            elif json_type == "object":
                py_type = typing.Dict[str, typing.Any]
            else:
                py_type = type_map.get(json_type, str)
            params.append(
                inspect.Parameter(
                    name=p_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    annotation=py_type,
                    default=None,
                )
            )

        # Provide common alias -> canonical key mapping so models can use flexible names
        def _build_alias_map(tool_name: str, schema: dict) -> dict[str, str]:
            aliases: dict[str, str] = {}
            canonical_keys = set(schema.keys())

            def add(canonical: str, *alias_names: str):
                if canonical not in canonical_keys:
                    return
                for a in alias_names:
                    if a not in canonical_keys:
                        aliases.setdefault(a, canonical)

            # Infer aliases solely from the presence of canonical params
            # Textual content
            lower_map = {k.lower(): k for k in canonical_keys}
            if "text" in lower_map:
                add(lower_map["text"], "content", "body", "markdown", "markdown_text")
            if "markdown_text" in lower_map:
                add(lower_map["markdown_text"], "markdown", "md", "content", "text")
            # Title/name
            if "title" in lower_map:
                add(lower_map["title"], "name", "subject")
            # Document identifiers
            if "document_id" in lower_map:
                add(lower_map["document_id"], "doc_id", "documentId", "docId", "id")
            # Email fields
            if "to" in lower_map:
                add(lower_map["to"], "recipient", "email", "to_email", "recipients")
            if "subject" in lower_map:
                add(lower_map["subject"], "title", "topic")
            if "body" in lower_map:
                add(lower_map["body"], "content", "text", "message")

            return aliases

        alias_map = _build_alias_map(tool.name, schema_props)
        for alias_name in alias_map.keys():
            params.append(
                inspect.Parameter(
                    name=alias_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    annotation=str,
                    default=None,
                )
            )

        async def tool_impl(**kwargs: Any) -> str:
            raw: Dict[str, Any] = {k: v for k, v in kwargs.items() if v is not None}
            payload: Dict[str, Any] = {}
            # Copy canonical keys first
            for k in schema_props.keys():
                if k in raw:
                    payload[k] = raw[k]
            # Then map aliases when canonical missing
            for alias_key, canonical_key in alias_map.items():
                if canonical_key not in payload and alias_key in raw:
                    payload[canonical_key] = raw[alias_key]
            input_json = json.dumps(payload)
            logger.info(f"Invoking fallback tool '{tool.name}' with payload: {payload}")
            result_str = await tool.on_invoke_tool(None, input_json)
            logger.info(f"Tool '{tool.name}' result: {result_str}")
            return result_str

        # Apply metadata and signature so the LLM knows available params
        tool_impl.__signature__ = inspect.Signature(parameters=params)
        tool_impl.__name__ = tool.name
        tool_impl.__doc__ = tool.description
        # Provide explicit annotations; default to str for broad compatibility
        annotations: dict[str, type] = {p.name: (p.annotation if p.annotation is not inspect._empty else str) for p in params}
        tool_impl.__annotations__ = {"return": str, **annotations}

        return function_tool()(tool_impl)

    @staticmethod
    def _register_alias_tool_names(prepared_tools: List[Callable], tool_instance: FunctionTool) -> None:
        # No-op: alias tool registration removed to keep tool count under model limits
        return

    @staticmethod
    async def register_with_agent(agent, mcp_servers: List[MCPServer],
                                 convert_schemas_to_strict: bool = True,
                                 auto_connect: bool = True) -> List[Callable]:
        """
        Helper method to prepare and register MCP tools with a LiveKit agent.

        Args:
            agent: The LiveKit agent instance
            mcp_servers: List of MCPServer instances
            convert_schemas_to_strict: Whether to convert schemas to strict format
            auto_connect: Whether to auto-connect to servers

        Returns:
            List of tool functions that were registered
        """
        # Prepare the dynamic tools
        tools = await MCPToolsIntegration.prepare_dynamic_tools(
            mcp_servers,
            convert_schemas_to_strict=convert_schemas_to_strict,
            auto_connect=auto_connect
        )

        # Register with the agent, respecting global model tool caps
        if hasattr(agent, '_tools') and isinstance(agent._tools, list):
            BASE_LIMIT = 120  # leave room for built-in tools
            available_slots = max(0, BASE_LIMIT - len(agent._tools))
            tools_to_add = tools[:available_slots]
            agent._tools.extend(tools_to_add)
            logger.info(f"Registered {len(tools_to_add)} MCP tools with agent (capped)")

            # Log the names of registered tools
            if tools_to_add:
                tool_names = [getattr(t, '__name__', 'unknown') for t in tools_to_add]
                logger.info(f"Registered tool names: {tool_names}")
        else:
            logger.warning("Agent does not have a '_tools' attribute, tools were not registered")

        return tools

    @staticmethod
    async def create_agent_with_tools(agent_class, mcp_servers: List[MCPServer], agent_kwargs: Dict = None,
                                    convert_schemas_to_strict: bool = True) -> Any:
        """
        Factory method to create and initialize an agent with MCP tools already loaded.

        Args:
            agent_class: Agent class to instantiate
            mcp_servers: List of MCP servers to register with the agent
            agent_kwargs: Additional keyword arguments to pass to the agent constructor
            convert_schemas_to_strict: Whether to convert JSON schemas to strict format

        Returns:
            An initialized agent instance with MCP tools registered
        """
        # Connect to MCP servers
        for server in mcp_servers:
            if not getattr(server, 'connected', False):
                try:
                    logger.debug(f"Connecting to MCP server: {server.name}")
                    await server.connect()
                except Exception as e:
                    logger.error(f"Failed to connect to MCP server {server.name}: {e}")

        # Create agent instance
        agent_kwargs = agent_kwargs or {}
        agent = agent_class(**agent_kwargs)

        # Prepare tools
        tools = await MCPToolsIntegration.prepare_dynamic_tools(
            mcp_servers,
            convert_schemas_to_strict=convert_schemas_to_strict,
            auto_connect=False  # Already connected above
        )

        # Register tools with agent
        if tools and hasattr(agent, '_tools') and isinstance(agent._tools, list):
            agent._tools.extend(tools)
            logger.info(f"Registered {len(tools)} MCP tools with agent")

            # Log the names of registered tools
            tool_names = [getattr(t, '__name__', 'unknown') for t in tools]
            logger.info(f"Registered tool names: {tool_names}")
        else:
            if not tools:
                logger.warning("No tools were found to register with the agent")
            else:
                logger.warning("Agent does not have a '_tools' attribute, tools were not registered")

        return agent
