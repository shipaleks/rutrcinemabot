"""Tests for Claude API client."""

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.claude_client import (
    ClaudeClient,
    ConversationContext,
    Message,
    ToolCall,
    ToolResult,
)
from src.ai.prompts import MEDIA_CONCIERGE_SYSTEM_PROMPT, get_system_prompt


class TestMessage:
    """Tests for Message dataclass."""

    def test_message_with_string_content(self):
        """Test creating message with string content."""
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"

    def test_message_with_list_content(self):
        """Test creating message with list content (for tool use)."""
        content = [{"type": "text", "text": "Hello"}]
        msg = Message(role="assistant", content=content)
        assert msg.role == "assistant"
        assert msg.content == content


class TestToolCall:
    """Tests for ToolCall dataclass."""

    def test_tool_call_creation(self):
        """Test creating a tool call."""
        tc = ToolCall(
            id="tool_123",
            name="search_movie",
            input={"query": "Dune"},
        )
        assert tc.id == "tool_123"
        assert tc.name == "search_movie"
        assert tc.input == {"query": "Dune"}


class TestToolResult:
    """Tests for ToolResult dataclass."""

    def test_tool_result_success(self):
        """Test creating successful tool result."""
        result = ToolResult(
            tool_use_id="tool_123",
            content='{"results": []}',
        )
        assert result.tool_use_id == "tool_123"
        assert result.content == '{"results": []}'
        assert result.is_error is False

    def test_tool_result_error(self):
        """Test creating error tool result."""
        result = ToolResult(
            tool_use_id="tool_123",
            content="Error: API unavailable",
            is_error=True,
        )
        assert result.is_error is True


class TestConversationContext:
    """Tests for ConversationContext."""

    def test_add_message(self):
        """Test adding messages to context."""
        ctx = ConversationContext()
        ctx.add_message("user", "Hello")
        ctx.add_message("assistant", "Hi there!")

        assert len(ctx.messages) == 2
        assert ctx.messages[0].role == "user"
        assert ctx.messages[1].role == "assistant"

    def test_message_trimming(self):
        """Test that old messages are trimmed."""
        ctx = ConversationContext(max_history=5)

        for i in range(10):
            ctx.add_message("user", f"Message {i}")

        assert len(ctx.messages) == 5
        assert ctx.messages[0].content == "Message 5"
        assert ctx.messages[-1].content == "Message 9"

    def test_get_messages_for_api(self):
        """Test formatting messages for API."""
        ctx = ConversationContext()
        ctx.add_message("user", "Hello")
        ctx.add_message("assistant", "Hi!")

        api_messages = ctx.get_messages_for_api()

        assert len(api_messages) == 2
        assert api_messages[0] == {"role": "user", "content": "Hello"}
        assert api_messages[1] == {"role": "assistant", "content": "Hi!"}

    def test_clear(self):
        """Test clearing conversation history."""
        ctx = ConversationContext()
        ctx.add_message("user", "Hello")
        ctx.clear()

        assert len(ctx.messages) == 0


class TestSystemPrompt:
    """Tests for system prompt generation."""

    def test_base_system_prompt(self):
        """Test getting base system prompt."""
        prompt = get_system_prompt()
        assert prompt == MEDIA_CONCIERGE_SYSTEM_PROMPT
        assert "медиа-консьерж" in prompt

    def test_system_prompt_with_preferences(self):
        """Test system prompt with user preferences."""
        preferences = {
            "preferred_quality": "4K",
            "preferred_language": "ru",
            "favorite_genres": ["sci-fi", "thriller"],
        }
        prompt = get_system_prompt(preferences)

        assert "медиа-консьерж" in prompt
        assert "4K" in prompt
        assert "русский" in prompt
        assert "sci-fi" in prompt

    def test_system_prompt_partial_preferences(self):
        """Test system prompt with partial preferences."""
        preferences = {"preferred_quality": "1080p"}
        prompt = get_system_prompt(preferences)

        assert "1080p" in prompt


class TestClaudeClient:
    """Tests for ClaudeClient."""

    @pytest.fixture
    def mock_anthropic(self):
        """Create a mock Anthropic client."""
        with patch("src.ai.claude_client.anthropic.AsyncAnthropic") as mock:
            yield mock

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for testing."""
        with patch("src.ai.claude_client.settings") as mock:
            mock.anthropic_api_key.get_secret_value.return_value = "test-api-key"
            yield mock

    def test_client_initialization(self, mock_anthropic, mock_settings):
        """Test client initialization."""
        client = ClaudeClient()

        assert client.model == "claude-sonnet-4-5-20250929"
        assert client.tools == []
        assert client.tool_executor is None

    def test_client_with_tools(self, mock_anthropic, mock_settings):
        """Test client initialization with tools."""
        tools = [
            {
                "name": "search",
                "description": "Search for movies",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]

        async def executor(name: str, args: dict[str, Any]) -> str:
            return "{}"

        client = ClaudeClient(tools=tools, tool_executor=executor)

        assert len(client.tools) == 1
        assert client.tool_executor is not None

    @pytest.mark.asyncio
    async def test_execute_tool_no_executor(self, mock_anthropic, mock_settings):
        """Test tool execution without executor returns error."""
        client = ClaudeClient()
        tool_call = ToolCall(id="1", name="search", input={})

        result = await client._execute_tool(tool_call)

        assert result.is_error is True
        assert "not configured" in result.content

    @pytest.mark.asyncio
    async def test_execute_tool_success(self, mock_anthropic, mock_settings):
        """Test successful tool execution."""

        async def executor(name: str, args: dict[str, Any]) -> str:
            return '{"results": ["movie1", "movie2"]}'

        client = ClaudeClient(tool_executor=executor)
        tool_call = ToolCall(id="1", name="search", input={"query": "test"})

        result = await client._execute_tool(tool_call)

        assert result.is_error is False
        assert "movie1" in result.content

    @pytest.mark.asyncio
    async def test_execute_tool_error(self, mock_anthropic, mock_settings):
        """Test tool execution with error."""

        async def executor(name: str, args: dict[str, Any]) -> str:
            raise ValueError("API error")

        client = ClaudeClient(tool_executor=executor)
        tool_call = ToolCall(id="1", name="search", input={})

        result = await client._execute_tool(tool_call)

        assert result.is_error is True
        assert "API error" in result.content

    @pytest.mark.asyncio
    async def test_simple_message(self, mock_anthropic, mock_settings):
        """Test simple message without context."""
        # Create mock response
        mock_text_block = MagicMock()
        mock_text_block.text = "Hello! How can I help?"

        mock_response = MagicMock()
        mock_response.content = [mock_text_block]

        mock_anthropic.return_value.messages.create = AsyncMock(return_value=mock_response)

        client = ClaudeClient()
        response = await client.simple_message("Hello")

        assert response == "Hello! How can I help?"

    @pytest.mark.asyncio
    async def test_send_message_basic(self, mock_anthropic, mock_settings):
        """Test sending a message with context."""
        # Create mock response (no tool use)
        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "Here's information about Dune."

        mock_response = MagicMock()
        mock_response.content = [mock_text_block]

        mock_anthropic.return_value.messages.create = AsyncMock(return_value=mock_response)

        client = ClaudeClient()
        context = ConversationContext()

        response = await client.send_message("Tell me about Dune", context)

        assert "Dune" in response
        # Check context was updated
        assert len(context.messages) == 2  # user + assistant

    @pytest.mark.asyncio
    async def test_send_message_with_tool_use(self, mock_anthropic, mock_settings):
        """Test sending a message that triggers tool use."""

        # First response: tool use
        @dataclass
        class MockToolUseBlock:
            type: str = "tool_use"
            id: str = "tool_1"
            name: str = "search_movie"
            input: dict = None

            def model_dump(self) -> dict:
                return {
                    "type": self.type,
                    "id": self.id,
                    "name": self.name,
                    "input": self.input or {},
                }

            def __post_init__(self):
                if self.input is None:
                    self.input = {"query": "Dune"}

        @dataclass
        class MockTextBlock:
            type: str = "text"
            text: str = "Found results for Dune"

            def model_dump(self) -> dict:
                return {"type": self.type, "text": self.text}

        tool_use_response = MagicMock()
        tool_use_response.content = [MockToolUseBlock()]

        text_response = MagicMock()
        text_response.content = [MockTextBlock()]

        # Mock create to return tool use first, then text
        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return tool_use_response
            return text_response

        mock_anthropic.return_value.messages.create = mock_create

        # Create tool executor
        async def executor(name: str, args: dict[str, Any]) -> str:
            return '{"title": "Dune", "year": 2021}'

        tools = [{"name": "search_movie", "description": "Search movies", "input_schema": {}}]
        client = ClaudeClient(tools=tools, tool_executor=executor)
        context = ConversationContext()

        response = await client.send_message("Find Dune movie", context)

        assert "Dune" in response
        assert call_count == 2  # Initial + after tool result


class TestStreamingMessage:
    """Tests for streaming message functionality."""

    @pytest.fixture
    def mock_anthropic(self):
        """Create a mock Anthropic client."""
        with patch("src.ai.claude_client.anthropic.AsyncAnthropic") as mock:
            yield mock

    @pytest.fixture
    def mock_settings(self):
        """Mock settings for testing."""
        with patch("src.ai.claude_client.settings") as mock:
            mock.anthropic_api_key.get_secret_value.return_value = "test-api-key"
            yield mock

    @pytest.mark.asyncio
    async def test_stream_basic_text(self, mock_anthropic, mock_settings):
        """Test streaming basic text response."""

        # Create mock stream events
        class MockTextDelta:
            text = "Hello "

        class MockTextDelta2:
            text = "World!"

        class MockEvent:
            def __init__(self, event_type, delta=None, content_block=None):
                self.type = event_type
                self.delta = delta
                self.content_block = content_block

        events = [
            MockEvent("content_block_delta", delta=MockTextDelta()),
            MockEvent("content_block_delta", delta=MockTextDelta2()),
        ]

        # Create async iterator
        async def mock_stream_iter():
            for event in events:
                yield event

        # Create mock stream context manager
        class MockStream:
            def __aiter__(self):
                return mock_stream_iter().__aiter__()

        class MockStreamContext:
            async def __aenter__(self):
                return MockStream()

            async def __aexit__(self, *args):
                pass

        mock_anthropic.return_value.messages.stream.return_value = MockStreamContext()

        client = ClaudeClient()
        context = ConversationContext()

        chunks = []
        async for chunk in client.stream_message("Hello", context):
            chunks.append(chunk)

        assert "".join(chunks) == "Hello World!"
