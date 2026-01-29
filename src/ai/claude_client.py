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

from src.ai.prompts import get_system_prompt_blocks
from src.config import settings

logger = structlog.get_logger(__name__)

# Fields that the SDK adds internally but are not accepted by the API
_EXCLUDED_BLOCK_FIELDS = {"parsed_output"}


def _dump_content_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """Serialize SDK content blocks for storage in conversation history.

    Strips SDK-internal fields (e.g. parsed_output) that the API rejects.
    """
    result = []
    for block in blocks:
        d = block.model_dump()
        for key in _EXCLUDED_BLOCK_FIELDS:
            d.pop(key, None)
        result.append(d)
    return result


def _add_cache_control_to_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add cache_control to the last tool definition for prompt caching.

    Anthropic's prompt caching caches everything up to and including the
    block marked with cache_control. By marking the last tool, we cache
    all tool definitions (which are static across requests).

    Args:
        tools: List of tool definitions.

    Returns:
        New list with cache_control on the last element.
    """
    if not tools:
        return tools
    # Shallow-copy the list; deep-copy only the last element to add cache_control
    result = list(tools)
    last = dict(result[-1])
    last["cache_control"] = {"type": "ephemeral"}
    result[-1] = last
    return result


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


def _estimate_tokens(content: str | list[dict[str, Any]]) -> int:
    """Estimate token count for a message content.

    Uses a rough heuristic of ~4 characters per token for text.
    For structured content blocks, sums up text and JSON representations.

    Args:
        content: Message content (text string or list of content blocks).

    Returns:
        Estimated token count.
    """
    if isinstance(content, str):
        return len(content) // 4 + 1

    total = 0
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type == "text":
                total += len(block.get("text", "")) // 4 + 1
            elif block_type == "tool_use":
                # tool name + JSON input
                total += 20  # overhead
                inp = block.get("input", {})
                total += len(json.dumps(inp, ensure_ascii=False)) // 4
            elif block_type == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    total += len(result_content) // 4 + 1
                else:
                    total += len(json.dumps(result_content, ensure_ascii=False)) // 4
            elif block_type in ("thinking", "redacted_thinking"):
                total += len(block.get("thinking", "")) // 4
            else:
                total += len(json.dumps(block, ensure_ascii=False)) // 4
        else:
            total += 10  # fallback
    return max(total, 1)


@dataclass
class ConversationContext:
    """Manages conversation history and context.

    Attributes:
        messages: List of messages in the conversation
        user_preferences: User's preferences for personalization
        user_profile_md: Full markdown profile for Claude's context (legacy)
        core_memory_content: Rendered core memory blocks (new MemGPT-style)
        telegram_user_id: Telegram user ID for tool calls
        max_history: Maximum number of messages to keep (safety cap)
        max_context_tokens: Approximate max tokens for conversation history
        context_loaded: Whether user profile/memory has been loaded from DB
    """

    messages: list[Message] = field(default_factory=list)
    user_preferences: dict[str, Any] | None = None
    user_profile_md: str | None = None
    core_memory_content: str | None = None
    telegram_user_id: int | None = None
    remember_requested: bool = False  # User explicitly asked to save (#запомни)
    max_history: int = 30  # Safety cap on message count
    max_context_tokens: int = 80_000  # Token budget for conversation history
    last_search_result_ids: list[str] = field(default_factory=list)  # For re-showing buttons
    context_loaded: bool = False  # Whether profile/memory loaded from DB

    def add_message(self, role: str, content: str | list[dict[str, Any]]) -> None:
        """Add a message to the conversation history.

        Args:
            role: Either "user" or "assistant"
            content: The message content
        """
        self.messages.append(Message(role=role, content=content))
        self._trim_history()

    def _trim_history(self) -> None:
        """Trim history by token budget, then by message count.

        Uses token-aware trimming: estimates total tokens in history and
        removes oldest messages (preferring to drop tool-heavy exchanges)
        until under budget. Also enforces a hard message count cap.

        Claude API requires every tool_result to have a corresponding tool_use
        in the previous message, so we always trim in pairs.
        """
        # Phase 1: trim by token budget
        self._trim_by_tokens()

        # Phase 2: trim by message count (safety cap)
        if len(self.messages) > self.max_history:
            start_idx = len(self.messages) - self.max_history
            # Ensure we don't break tool_use/tool_result pairs
            start_idx = self._safe_trim_index(start_idx)
            self.messages = self.messages[start_idx:]

    def _trim_by_tokens(self) -> None:
        """Remove oldest messages until total history is under token budget."""
        total_tokens = sum(_estimate_tokens(msg.content) for msg in self.messages)

        if total_tokens <= self.max_context_tokens:
            return

        logger.info(
            "trimming_history_by_tokens",
            total_tokens=total_tokens,
            budget=self.max_context_tokens,
            message_count=len(self.messages),
        )

        # Remove messages from the front until under budget
        while len(self.messages) > 2 and total_tokens > self.max_context_tokens:
            # Calculate how many messages to skip from the front
            removed = self.messages.pop(0)
            total_tokens -= _estimate_tokens(removed.content)

            # If we just removed an assistant message with tool_use, the next
            # message might be a tool_result which is now orphaned — remove it too
            if (
                self.messages
                and self.messages[0].role == "user"
                and self._has_tool_result(self.messages[0].content)
            ):
                removed2 = self.messages.pop(0)
                total_tokens -= _estimate_tokens(removed2.content)

            # If we removed a tool_result (user msg), the preceding assistant
            # tool_use is already gone, but check the new first message
            if (
                self.messages
                and self.messages[0].role == "user"
                and self._has_tool_result(self.messages[0].content)
            ):
                removed3 = self.messages.pop(0)
                total_tokens -= _estimate_tokens(removed3.content)

        logger.info(
            "history_trimmed_by_tokens",
            new_total_tokens=total_tokens,
            new_message_count=len(self.messages),
        )

    def _safe_trim_index(self, start_idx: int) -> int:
        """Find a safe trim index that doesn't break tool_use/tool_result pairs."""
        while start_idx > 0:
            first_msg = self.messages[start_idx]
            if first_msg.role == "user" and self._has_tool_result(first_msg.content):
                start_idx -= 1
            else:
                break
        return start_idx

    def _has_tool_result(self, content: str | list[dict[str, Any]]) -> bool:
        """Check if message content contains tool_result blocks."""
        if isinstance(content, str):
            return False
        return any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )

    def has_thinking_compatible_history(self) -> bool:
        """Check if conversation history is compatible with extended thinking.

        Returns True if either:
        - No assistant messages in history
        - All assistant messages already have thinking/redacted_thinking blocks

        Returns:
            True if thinking can be enabled, False if history lacks thinking blocks.
        """
        for msg in self.messages:
            if msg.role == "assistant":
                content = msg.content
                if isinstance(content, str):
                    # Text-only message without thinking block
                    return False
                if isinstance(content, list) and content:
                    first_type = content[0].get("type", "") if isinstance(content[0], dict) else ""
                    if first_type not in ("thinking", "redacted_thinking"):
                        return False
        return True

    def get_messages_for_api(self, strip_thinking: bool = False) -> list[dict[str, Any]]:
        """Get messages formatted for the Anthropic API.

        Args:
            strip_thinking: If True, remove thinking blocks from messages.
                           Required when continuing without thinking enabled.

        Returns:
            List of message dicts ready for the API.
        """
        messages = []
        for msg in self.messages:
            if strip_thinking and isinstance(msg.content, list):
                # Filter out thinking blocks and redacted_thinking blocks
                filtered_content = [
                    block
                    for block in msg.content
                    if not (
                        isinstance(block, dict)
                        and block.get("type") in ("thinking", "redacted_thinking")
                    )
                ]
                # Skip empty messages after filtering
                if filtered_content:
                    messages.append({"role": msg.role, "content": filtered_content})
            else:
                messages.append({"role": msg.role, "content": msg.content})
        return messages

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

        # Get system prompt as content blocks with cache_control for prompt caching
        system_blocks = get_system_prompt_blocks(
            user_preferences=context.user_preferences,
            user_profile_md=context.user_profile_md,
            core_memory_content=context.core_memory_content,
            remember_requested=context.remember_requested,
        )

        # Check if thinking can be enabled (requires compatible history)
        effective_thinking_budget = self.thinking_budget
        if effective_thinking_budget > 0 and not context.has_thinking_compatible_history():
            logger.warning(
                "thinking_disabled_incompatible_history",
                requested_budget=self.thinking_budget,
            )
            effective_thinking_budget = 0

        # Prepare API call parameters with prompt caching
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": context.get_messages_for_api(),
        }

        # Add tools with cache_control on last element for prompt caching
        if self.tools:
            params["tools"] = _add_cache_control_to_tools(self.tools)

        # Add extended thinking if enabled and history is compatible
        if effective_thinking_budget > 0:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": effective_thinking_budget,
            }
            # Extended thinking requires higher max_tokens
            params["max_tokens"] = max(max_tokens, effective_thinking_budget + 4096)

        logger.debug(
            "sending_message",
            message_length=len(user_message),
            history_length=len(context.messages),
            thinking_enabled=effective_thinking_budget > 0,
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

                # Add assistant response to context, preserving thinking blocks
                # Check if response has thinking blocks
                has_thinking = any(block.type == "thinking" for block in response.content)
                if has_thinking:
                    # Preserve all content blocks for thinking compatibility
                    context.add_message(
                        "assistant",
                        _dump_content_blocks(response.content),
                    )
                else:
                    # Just text, save as string
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
                _dump_content_blocks(response.content),
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
            # Check if response has thinking blocks to decide whether to keep thinking enabled
            has_thinking = any(block.type == "thinking" for block in response.content)
            continuation_params = params.copy()
            # Only disable thinking if response didn't have thinking blocks
            if "thinking" in continuation_params and not has_thinking:
                del continuation_params["thinking"]
                continuation_params["max_tokens"] = 16384
                # Strip thinking blocks from messages when thinking is disabled
                continuation_params["messages"] = context.get_messages_for_api(strip_thinking=True)
            else:
                continuation_params["messages"] = context.get_messages_for_api()
            response = await self.client.messages.create(**continuation_params)

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

        # Get system prompt as content blocks with cache_control for prompt caching
        system_blocks = get_system_prompt_blocks(
            user_preferences=context.user_preferences,
            user_profile_md=context.user_profile_md,
            core_memory_content=context.core_memory_content,
            remember_requested=context.remember_requested,
        )

        # Check if thinking can be enabled (requires compatible history)
        effective_thinking_budget = self.thinking_budget
        if effective_thinking_budget > 0 and not context.has_thinking_compatible_history():
            logger.warning(
                "thinking_disabled_incompatible_history_stream",
                requested_budget=self.thinking_budget,
            )
            effective_thinking_budget = 0

        # Prepare API call parameters with prompt caching
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": context.get_messages_for_api(),
        }

        # Add tools with cache_control on last element for prompt caching
        if self.tools:
            params["tools"] = _add_cache_control_to_tools(self.tools)

        # Add extended thinking if enabled and history is compatible
        if effective_thinking_budget > 0:
            params["thinking"] = {
                "type": "enabled",
                "budget_tokens": effective_thinking_budget,
            }
            # Extended thinking requires higher max_tokens
            params["max_tokens"] = max(max_tokens, effective_thinking_budget + 4096)

        logger.debug(
            "starting_stream",
            message_length=len(user_message),
            history_length=len(context.messages),
            thinking_enabled=effective_thinking_budget > 0,
        )

        try:
            accumulated_text = ""
            tool_calls: list[dict[str, Any]] = []
            current_tool_input = ""
            current_tool_id = ""
            current_tool_name = ""
            final_message = None

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
                        # Note: thinking deltas are handled automatically

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

                # Get final message with all content blocks (including thinking with signatures)
                final_message = await stream.get_final_message()

            # Check if response has thinking blocks (for continuation decision)
            has_thinking = (
                any(block.type == "thinking" for block in final_message.content)
                if final_message
                else False
            )

            # After stream completes, handle tool calls if any
            if tool_calls:
                logger.info(
                    "stream_tool_calls",
                    count=len(tool_calls),
                )

                # Add assistant response to context using final_message content
                # This preserves thinking blocks with their signatures
                context.add_message(
                    "assistant",
                    _dump_content_blocks(final_message.content),
                )

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

                # Continue conversation - keep thinking if response had thinking blocks
                continuation_params = params.copy()
                # Only disable thinking if response didn't have thinking blocks
                if "thinking" in continuation_params and not has_thinking:
                    del continuation_params["thinking"]
                    continuation_params["max_tokens"] = 16384
                    # Strip thinking blocks from messages when thinking is disabled
                    continuation_params["messages"] = context.get_messages_for_api(
                        strip_thinking=True
                    )
                else:
                    continuation_params["messages"] = context.get_messages_for_api()
                response = await self.client.messages.create(**continuation_params)

                # Extract final text from continued response
                final_text = await self._process_response(response, context, params)

                # Yield the continuation (excluding what was already yielded)
                if final_text and final_text != accumulated_text:
                    yield "\n\n" + final_text
            else:
                # No tool calls, add response to context
                # Use final_message to preserve thinking blocks with signatures
                if final_message and final_message.content:
                    if has_thinking:
                        # Preserve all content blocks including thinking with signatures
                        context.add_message(
                            "assistant",
                            _dump_content_blocks(final_message.content),
                        )
                    elif accumulated_text:
                        # No thinking, just save text
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
