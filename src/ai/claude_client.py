"""Claude API client with streaming and tool_use support.

This module provides an async client for interacting with Anthropic's Claude API,
including support for streaming responses and function calling via tools.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
import structlog

from src.ai.prompts import get_system_prompt
from src.config import settings

logger = structlog.get_logger(__name__)

# Default Claude model
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5-20250929"


@dataclass
class Message:
    """Represents a message in the conversation.

    Attributes:
        role: Either "user" or "assistant"
        content: The message content (text or list of content blocks)
    """

    role: str
    content: str | list[dict[str, Any]]


@dataclass
class ToolCall:
    """Represents a tool call requested by Claude.

    Attributes:
        id: Unique identifier for the tool call
        name: Name of the tool to invoke
        input: Input parameters for the tool
    """

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    """Represents the result of a tool execution.

    Attributes:
        tool_use_id: ID of the tool call this is a response to
        content: The result content (string or error message)
        is_error: Whether the result is an error
    """

    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class ConversationContext:
    """Manages conversation history and context.

    Attributes:
        messages: List of messages in the conversation
        user_preferences: User's preferences for personalization
        user_profile_md: Full markdown profile for Claude's context (legacy)
        core_memory_content: Rendered core memory blocks (new MemGPT-style)
        telegram_user_id: Telegram user ID for tool calls
        max_history: Maximum number of messages to keep
    """

    messages: list[Message] = field(default_factory=list)
    user_preferences: dict[str, Any] | None = None
    user_profile_md: str | None = None
    core_memory_content: str | None = None
    telegram_user_id: int | None = None
    max_history: int = 20

    def add_message(self, role: str, content: str | list[dict[str, Any]]) -> None:
        """Add a message to the conversation history.

        Args:
            role: Either "user" or "assistant"
            content: The message content
        """
        self.messages.append(Message(role=role, content=content))

        # Trim history if too long
        if len(self.messages) > self.max_history:
            # Keep system-relevant messages and recent history
            self.messages = self.messages[-self.max_history :]

    def get_messages_for_api(self) -> list[dict[str, Any]]:
        """Get messages formatted for the Anthropic API.

        Returns:
            List of message dicts ready for the API.
        """
        return [{"role": msg.role, "content": msg.content} for msg in self.messages]

    def clear(self) -> None:
        """Clear the conversation history."""
        self.messages.clear()


class ClaudeClient:
    """Async client for Claude API with streaming and tool support.

    This client handles:
    - Streaming responses for progressive message delivery
    - Tool/function calling via Claude's tool_use feature
    - Conversation history management
    - Error handling and retries
    """

    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[str]] | None = None,
        model: str | None = None,
        thinking_budget: int = 0,
    ):
        """Initialize the Claude client.

        Args:
            tools: List of tool definitions for Claude to use
            tool_executor: Async function to execute tool calls
            model: Claude model ID to use (default: claude-sonnet-4-5-20250929)
            thinking_budget: Extended thinking budget in tokens (0 = disabled)
        """
        self.client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
        self.tools = tools or []
        self.tool_executor = tool_executor
        self.model = model or DEFAULT_CLAUDE_MODEL
        self.thinking_budget = thinking_budget

        logger.info(
            "claude_client_initialized",
            model=self.model,
            thinking_budget=self.thinking_budget,
            tools_count=len(self.tools),
        )

    async def send_message(
        self,
        user_message: str,
        context: ConversationContext,
        max_tokens: int = 4096,
    ) -> str:
        """Send a message to Claude and get a complete response.

        This method handles the full conversation loop including tool calls.

        Args:
            user_message: The user's message
            context: Conversation context with history
            max_tokens: Maximum tokens in the response

        Returns:
            Claude's text response.
        """
        # Add user message to context
        context.add_message("user", user_message)

        # Get system prompt with user preferences and profile
        system_prompt = get_system_prompt(
            user_preferences=context.user_preferences,
            user_profile_md=context.user_profile_md,
            core_memory_content=context.core_memory_content,
        )

        # Prepare API call parameters
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": context.get_messages_for_api(),
        }

        # Add tools if available
        if self.tools:
            params["tools"] = self.tools

        # Add extended thinking if enabled
        if self.thinking_budget > 0:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget,
            }
            # Extended thinking requires higher max_tokens
            params["max_tokens"] = max(max_tokens, self.thinking_budget + 4096)

        logger.debug(
            "sending_message",
            message_length=len(user_message),
            history_length=len(context.messages),
            thinking_enabled=self.thinking_budget > 0,
        )

        try:
            # Initial API call
            response = await self.client.messages.create(**params)

            # Process response, handling tool calls
            return await self._process_response(response, context, params)

        except anthropic.APIConnectionError as e:
            logger.error("api_connection_error", error=str(e))
            raise
        except anthropic.RateLimitError as e:
            logger.error("rate_limit_error", error=str(e))
            raise
        except anthropic.APIStatusError as e:
            logger.error(
                "api_status_error",
                status_code=e.status_code,
                error=str(e),
            )
            raise

    async def _process_response(
        self,
        response: anthropic.types.Message,
        context: ConversationContext,
        params: dict[str, Any],
    ) -> str:
        """Process Claude's response, handling tool calls if needed.

        Args:
            response: The API response
            context: Conversation context
            params: Original API parameters for retries

        Returns:
            Final text response after all tool calls are processed.
        """
        max_tool_iterations = 10
        iteration = 0

        while iteration < max_tool_iterations:
            iteration += 1

            # Check if we have tool use in the response
            tool_calls = [block for block in response.content if block.type == "tool_use"]

            if not tool_calls:
                # No tool calls, extract text and return
                text_blocks = [block.text for block in response.content if hasattr(block, "text")]
                final_text = "\n".join(text_blocks)

                # Add assistant response to context
                context.add_message("assistant", final_text)

                logger.info(
                    "response_complete",
                    text_length=len(final_text),
                    iterations=iteration,
                )

                return final_text

            # Process tool calls
            logger.info(
                "processing_tool_calls",
                count=len(tool_calls),
                iteration=iteration,
            )

            # Add assistant's response (with tool_use) to context
            context.add_message(
                "assistant",
                [block.model_dump() for block in response.content],
            )

            # Execute tools and collect results
            tool_results = []
            for tool_block in tool_calls:
                tool_call = ToolCall(
                    id=tool_block.id,
                    name=tool_block.name,
                    input=tool_block.input,
                )
                result = await self._execute_tool(tool_call)
                tool_results.append(result)

            # Add tool results to context
            context.add_message(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": result.tool_use_id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                    for result in tool_results
                ],
            )

            # Update params and continue conversation
            params["messages"] = context.get_messages_for_api()
            response = await self.client.messages.create(**params)

        # Max iterations reached
        logger.warning(
            "max_tool_iterations_reached",
            max_iterations=max_tool_iterations,
        )
        return "Произошла ошибка при обработке запроса. Пожалуйста, попробуйте ещё раз."

    async def _execute_tool(self, tool_call: ToolCall) -> ToolResult:
        """Execute a tool call and return the result.

        Args:
            tool_call: The tool call to execute

        Returns:
            ToolResult with the execution output or error.
        """
        logger.info(
            "executing_tool",
            tool_name=tool_call.name,
            tool_id=tool_call.id,
        )

        if not self.tool_executor:
            return ToolResult(
                tool_use_id=tool_call.id,
                content="Tool executor not configured",
                is_error=True,
            )

        try:
            result = await self.tool_executor(tool_call.name, tool_call.input)
            logger.info(
                "tool_executed",
                tool_name=tool_call.name,
                result_length=len(result),
            )
            return ToolResult(
                tool_use_id=tool_call.id,
                content=result,
            )
        except Exception as e:
            logger.error(
                "tool_execution_error",
                tool_name=tool_call.name,
                error=str(e),
            )
            return ToolResult(
                tool_use_id=tool_call.id,
                content=f"Error executing tool: {str(e)}",
                is_error=True,
            )

    async def stream_message(
        self,
        user_message: str,
        context: ConversationContext,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Stream a message response from Claude.

        This method yields text chunks as they are generated, suitable for
        progressive message updates in Telegram.

        Note: Tool calls are handled internally; only text is yielded.

        Args:
            user_message: The user's message
            context: Conversation context with history
            max_tokens: Maximum tokens in the response

        Yields:
            Text chunks as they are generated.
        """
        # Add user message to context
        context.add_message("user", user_message)

        # Get system prompt with user preferences and profile
        system_prompt = get_system_prompt(
            user_preferences=context.user_preferences,
            user_profile_md=context.user_profile_md,
            core_memory_content=context.core_memory_content,
        )

        # Prepare API call parameters
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": context.get_messages_for_api(),
        }

        # Add tools if available
        if self.tools:
            params["tools"] = self.tools

        # Add extended thinking if enabled
        if self.thinking_budget > 0:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget,
            }
            # Extended thinking requires higher max_tokens
            params["max_tokens"] = max(max_tokens, self.thinking_budget + 4096)

        logger.debug(
            "starting_stream",
            message_length=len(user_message),
            history_length=len(context.messages),
            thinking_enabled=self.thinking_budget > 0,
        )

        try:
            accumulated_text = ""
            tool_calls: list[dict[str, Any]] = []
            current_tool_input = ""
            current_tool_id = ""
            current_tool_name = ""

            async with self.client.messages.stream(**params) as stream:
                async for event in stream:
                    # Handle different event types
                    if event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            current_tool_id = event.content_block.id
                            current_tool_name = event.content_block.name
                            current_tool_input = ""

                    elif event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            # Text delta - yield it
                            text = event.delta.text
                            accumulated_text += text
                            yield text
                        elif hasattr(event.delta, "partial_json"):
                            # Tool input delta - accumulate
                            current_tool_input += event.delta.partial_json

                    elif (
                        event.type == "content_block_stop" and current_tool_id and current_tool_name
                    ):
                        # Finalize tool call
                        try:
                            tool_input = (
                                json.loads(current_tool_input) if current_tool_input else {}
                            )
                        except json.JSONDecodeError:
                            tool_input = {}

                        tool_calls.append(
                            {
                                "id": current_tool_id,
                                "name": current_tool_name,
                                "input": tool_input,
                            }
                        )
                        current_tool_id = ""
                        current_tool_name = ""

            # After stream completes, handle tool calls if any
            if tool_calls:
                logger.info(
                    "stream_tool_calls",
                    count=len(tool_calls),
                )

                # Add assistant response with tool calls to context
                content_blocks = []
                if accumulated_text:
                    content_blocks.append({"type": "text", "text": accumulated_text})
                for tc in tool_calls:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["input"],
                        }
                    )
                context.add_message("assistant", content_blocks)

                # Execute tools
                tool_results = []
                for tc in tool_calls:
                    tool_call = ToolCall(
                        id=tc["id"],
                        name=tc["name"],
                        input=tc["input"],
                    )
                    result = await self._execute_tool(tool_call)
                    tool_results.append(result)

                # Add tool results to context
                context.add_message(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": result.tool_use_id,
                            "content": result.content,
                            "is_error": result.is_error,
                        }
                        for result in tool_results
                    ],
                )

                # Continue conversation (non-streaming for tool continuations)
                params["messages"] = context.get_messages_for_api()
                response = await self.client.messages.create(**params)

                # Extract final text from continued response
                final_text = await self._process_response(response, context, params)

                # Yield the continuation (excluding what was already yielded)
                if final_text and final_text != accumulated_text:
                    yield "\n\n" + final_text
            else:
                # No tool calls, add text to context
                if accumulated_text:
                    context.add_message("assistant", accumulated_text)

            logger.info(
                "stream_complete",
                total_length=len(accumulated_text),
            )

        except anthropic.APIConnectionError as e:
            logger.error("stream_connection_error", error=str(e))
            raise
        except anthropic.RateLimitError as e:
            logger.error("stream_rate_limit_error", error=str(e))
            raise
        except anthropic.APIStatusError as e:
            logger.error(
                "stream_status_error",
                status_code=e.status_code,
                error=str(e),
            )
            raise

    async def simple_message(
        self,
        message: str,
        system_prompt: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        """Send a simple one-off message without conversation context.

        Useful for quick queries that don't need history or tools.

        Args:
            message: The user's message
            system_prompt: Optional custom system prompt
            max_tokens: Maximum tokens in the response

        Returns:
            Claude's text response.
        """
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": message}],
        }

        if system_prompt:
            params["system"] = system_prompt

        response = await self.client.messages.create(**params)

        text_blocks = [block.text for block in response.content if hasattr(block, "text")]
        return "\n".join(text_blocks)
