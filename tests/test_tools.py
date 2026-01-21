"""Tests for the tools module.

Tests cover:
- Tool definitions structure and validity
- Tool executor registration and execution
- Input validation
- Error handling
"""

import json

import pytest

from src.ai.tools import (
    ALL_TOOLS,
    GET_USER_PROFILE_TOOL,
    KINOPOISK_SEARCH_TOOL,
    PIRATEBAY_SEARCH_TOOL,
    RUTRACKER_SEARCH_TOOL,
    SEEDBOX_DOWNLOAD_TOOL,
    TMDB_CREDITS_TOOL,
    TMDB_SEARCH_TOOL,
    ToolExecutor,
    create_executor_with_stubs,
    get_tool_by_name,
    get_tool_definitions,
    stub_handler,
    validate_tool_input,
)

# =============================================================================
# Tool Definitions Tests
# =============================================================================


class TestToolDefinitions:
    """Tests for tool definition structure and validity."""

    def test_all_tools_count(self) -> None:
        """Verify all required tools are defined."""
        # We have 22 base tools + 4 memory system tools = 26 total
        assert len(ALL_TOOLS) >= 22  # At minimum, we have these many tools

    def test_all_tools_have_required_fields(self) -> None:
        """Each tool must have name, description, and input_schema."""
        for tool in ALL_TOOLS:
            assert "name" in tool, f"Tool missing name: {tool}"
            assert "description" in tool, f"Tool {tool.get('name')} missing description"
            assert "input_schema" in tool, f"Tool {tool['name']} missing input_schema"

    def test_all_tools_have_valid_schema(self) -> None:
        """Each tool's input_schema must be a valid JSON schema object."""
        for tool in ALL_TOOLS:
            schema = tool["input_schema"]
            assert schema["type"] == "object", f"Tool {tool['name']} schema must be object"
            assert "properties" in schema, f"Tool {tool['name']} missing properties"
            assert "required" in schema, f"Tool {tool['name']} missing required"

    def test_tool_names_are_unique(self) -> None:
        """All tool names must be unique."""
        names = [tool["name"] for tool in ALL_TOOLS]
        assert len(names) == len(set(names)), "Duplicate tool names found"

    def test_expected_tools_exist(self) -> None:
        """Verify all expected tools are defined."""
        expected_tools = [
            "rutracker_search",
            "piratebay_search",
            "tmdb_search",
            "tmdb_credits",
            "kinopoisk_search",
            "get_user_profile",
            "seedbox_download",
        ]
        actual_names = [tool["name"] for tool in ALL_TOOLS]
        for expected in expected_tools:
            assert expected in actual_names, f"Missing expected tool: {expected}"


class TestIndividualTools:
    """Tests for individual tool definitions."""

    def test_rutracker_search_schema(self) -> None:
        """Verify rutracker_search has correct schema."""
        tool = RUTRACKER_SEARCH_TOOL
        assert tool["name"] == "rutracker_search"
        assert "query" in tool["input_schema"]["properties"]
        assert "quality" in tool["input_schema"]["properties"]
        assert "category" in tool["input_schema"]["properties"]
        assert "query" in tool["input_schema"]["required"]
        # Quality must have enum
        quality_prop = tool["input_schema"]["properties"]["quality"]
        assert "enum" in quality_prop
        assert "1080p" in quality_prop["enum"]
        assert "4K" in quality_prop["enum"]

    def test_piratebay_search_schema(self) -> None:
        """Verify piratebay_search has correct schema."""
        tool = PIRATEBAY_SEARCH_TOOL
        assert tool["name"] == "piratebay_search"
        assert "query" in tool["input_schema"]["properties"]
        assert "min_seeds" in tool["input_schema"]["properties"]
        assert tool["input_schema"]["properties"]["min_seeds"]["type"] == "integer"

    def test_tmdb_search_schema(self) -> None:
        """Verify tmdb_search has correct schema."""
        tool = TMDB_SEARCH_TOOL
        assert tool["name"] == "tmdb_search"
        assert "query" in tool["input_schema"]["properties"]
        assert "year" in tool["input_schema"]["properties"]
        assert "media_type" in tool["input_schema"]["properties"]
        media_type_prop = tool["input_schema"]["properties"]["media_type"]
        assert "movie" in media_type_prop["enum"]
        assert "tv" in media_type_prop["enum"]

    def test_tmdb_credits_schema(self) -> None:
        """Verify tmdb_credits has correct schema."""
        tool = TMDB_CREDITS_TOOL
        assert tool["name"] == "tmdb_credits"
        assert "tmdb_id" in tool["input_schema"]["required"]
        assert "media_type" in tool["input_schema"]["required"]
        assert tool["input_schema"]["properties"]["tmdb_id"]["type"] == "integer"

    def test_kinopoisk_search_schema(self) -> None:
        """Verify kinopoisk_search has correct schema."""
        tool = KINOPOISK_SEARCH_TOOL
        assert tool["name"] == "kinopoisk_search"
        assert "query" in tool["input_schema"]["required"]
        assert "year" in tool["input_schema"]["properties"]

    def test_get_user_profile_schema(self) -> None:
        """Verify get_user_profile has correct schema."""
        tool = GET_USER_PROFILE_TOOL
        assert tool["name"] == "get_user_profile"
        assert "user_id" in tool["input_schema"]["required"]
        assert tool["input_schema"]["properties"]["user_id"]["type"] == "integer"

    def test_seedbox_download_schema(self) -> None:
        """Verify seedbox_download has correct schema."""
        tool = SEEDBOX_DOWNLOAD_TOOL
        assert tool["name"] == "seedbox_download"
        assert "magnet" in tool["input_schema"]["required"]
        assert "user_id" in tool["input_schema"]["required"]
        assert "name" in tool["input_schema"]["properties"]


# =============================================================================
# Helper Functions Tests
# =============================================================================


class TestHelperFunctions:
    """Tests for tool helper functions."""

    def test_get_tool_definitions_returns_copy(self) -> None:
        """get_tool_definitions should return a copy, not the original."""
        tools1 = get_tool_definitions()
        tools2 = get_tool_definitions()
        assert tools1 is not tools2
        assert tools1 == tools2

    def test_get_tool_by_name_found(self) -> None:
        """get_tool_by_name should return tool when found."""
        tool = get_tool_by_name("tmdb_search")
        assert tool is not None
        assert tool["name"] == "tmdb_search"

    def test_get_tool_by_name_not_found(self) -> None:
        """get_tool_by_name should return None when not found."""
        tool = get_tool_by_name("nonexistent_tool")
        assert tool is None


# =============================================================================
# Tool Validation Tests
# =============================================================================


class TestToolValidation:
    """Tests for tool input validation."""

    def test_validate_valid_input(self) -> None:
        """Valid input should return empty errors list."""
        errors = validate_tool_input(
            "tmdb_search",
            {"query": "Dune", "year": 2021, "media_type": "movie"},
        )
        assert errors == []

    def test_validate_missing_required_field(self) -> None:
        """Missing required field should return error."""
        errors = validate_tool_input("tmdb_search", {"year": 2021})
        assert len(errors) == 1
        assert "query" in errors[0]

    def test_validate_invalid_enum_value(self) -> None:
        """Invalid enum value should return error."""
        errors = validate_tool_input(
            "tmdb_search",
            {"query": "Dune", "media_type": "invalid"},
        )
        assert len(errors) == 1
        assert "media_type" in errors[0]
        assert "movie" in errors[0]

    def test_validate_invalid_type_string(self) -> None:
        """String field with wrong type should return error."""
        errors = validate_tool_input(
            "tmdb_search",
            {"query": 123},  # Should be string
        )
        assert len(errors) == 1
        assert "string" in errors[0]

    def test_validate_invalid_type_integer(self) -> None:
        """Integer field with wrong type should return error."""
        errors = validate_tool_input(
            "tmdb_search",
            {"query": "Dune", "year": "2021"},  # Should be integer
        )
        assert len(errors) == 1
        assert "integer" in errors[0]

    def test_validate_unknown_tool(self) -> None:
        """Unknown tool should return error."""
        errors = validate_tool_input("unknown_tool", {"foo": "bar"})
        assert len(errors) == 1
        assert "Unknown tool" in errors[0]

    def test_validate_multiple_errors(self) -> None:
        """Multiple validation errors should all be returned."""
        errors = validate_tool_input(
            "tmdb_credits",
            {},  # Missing both required fields
        )
        assert len(errors) == 2
        assert any("tmdb_id" in e for e in errors)
        assert any("media_type" in e for e in errors)


# =============================================================================
# Tool Executor Tests
# =============================================================================


class TestToolExecutor:
    """Tests for ToolExecutor class."""

    def test_executor_initialization(self) -> None:
        """Executor should initialize with empty handlers."""
        executor = ToolExecutor()
        assert executor.get_registered_tools() == []

    def test_register_handler(self) -> None:
        """Handler registration should work correctly."""

        async def mock_handler(input_data: dict) -> str:
            return "result"

        executor = ToolExecutor()
        executor.register_handler("tmdb_search", mock_handler)

        assert executor.has_handler("tmdb_search")
        assert not executor.has_handler("other_tool")
        assert "tmdb_search" in executor.get_registered_tools()

    def test_register_handlers_multiple(self) -> None:
        """Multiple handlers can be registered at once."""

        async def handler1(input_data: dict) -> str:
            return "result1"

        async def handler2(input_data: dict) -> str:
            return "result2"

        executor = ToolExecutor()
        executor.register_handlers(
            {
                "tmdb_search": handler1,
                "kinopoisk_search": handler2,
            }
        )

        assert executor.has_handler("tmdb_search")
        assert executor.has_handler("kinopoisk_search")
        assert len(executor.get_registered_tools()) == 2

    @pytest.mark.asyncio
    async def test_execute_success(self) -> None:
        """Tool execution should work correctly."""

        async def mock_handler(input_data: dict) -> str:
            return f"searched for: {input_data['query']}"

        executor = ToolExecutor()
        executor.register_handler("tmdb_search", mock_handler)

        result = await executor.execute("tmdb_search", {"query": "Dune"})
        assert result == "searched for: Dune"

    @pytest.mark.asyncio
    async def test_execute_callable(self) -> None:
        """Executor should be callable for ClaudeClient integration."""

        async def mock_handler(input_data: dict) -> str:
            return "result"

        executor = ToolExecutor()
        executor.register_handler("tmdb_search", mock_handler)

        # Test __call__ method
        result = await executor("tmdb_search", {"query": "test"})
        assert result == "result"

    @pytest.mark.asyncio
    async def test_execute_no_handler(self) -> None:
        """Execution without handler should raise ValueError."""
        executor = ToolExecutor()

        with pytest.raises(ValueError) as exc_info:
            await executor.execute("unknown_tool", {"foo": "bar"})

        assert "No handler registered" in str(exc_info.value)
        assert "unknown_tool" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_handler_exception(self) -> None:
        """Handler exception should propagate."""

        async def failing_handler(input_data: dict) -> str:
            raise RuntimeError("Handler failed")

        executor = ToolExecutor()
        executor.register_handler("tmdb_search", failing_handler)

        with pytest.raises(RuntimeError) as exc_info:
            await executor.execute("tmdb_search", {"query": "test"})

        assert "Handler failed" in str(exc_info.value)


class TestStubHandler:
    """Tests for stub handler and executor with stubs."""

    @pytest.mark.asyncio
    async def test_stub_handler_returns_json(self) -> None:
        """Stub handler should return JSON with input."""
        result = await stub_handler({"query": "Dune", "year": 2021})
        parsed = json.loads(result)

        assert parsed["status"] == "stub"
        assert "received_input" in parsed
        assert parsed["received_input"]["query"] == "Dune"
        assert parsed["received_input"]["year"] == 2021

    def test_create_executor_with_stubs(self) -> None:
        """create_executor_with_stubs should register all tools."""
        executor = create_executor_with_stubs()

        for tool in ALL_TOOLS:
            assert executor.has_handler(tool["name"]), f"Missing handler for {tool['name']}"

    @pytest.mark.asyncio
    async def test_executor_with_stubs_works(self) -> None:
        """Executor with stubs should execute all tools."""
        executor = create_executor_with_stubs()

        result = await executor.execute("tmdb_search", {"query": "Dune"})
        parsed = json.loads(result)

        assert parsed["status"] == "stub"
        assert parsed["received_input"]["query"] == "Dune"


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests for tools module."""

    def test_all_tools_can_be_imported(self) -> None:
        """All tool exports should be importable."""
        from src.ai import (
            ALL_TOOLS,
            ToolExecutor,
            create_executor_with_stubs,
            get_tool_by_name,
            get_tool_definitions,
            validate_tool_input,
        )

        assert ALL_TOOLS is not None
        assert ToolExecutor is not None
        assert callable(create_executor_with_stubs)
        assert callable(get_tool_by_name)
        assert callable(get_tool_definitions)
        assert callable(validate_tool_input)

    @pytest.mark.asyncio
    async def test_executor_integration_with_claude_client_signature(self) -> None:
        """Executor should match ClaudeClient's expected tool_executor signature."""
        executor = create_executor_with_stubs()

        # ClaudeClient expects: tool_executor(tool_name: str, tool_input: dict) -> str
        # Test the signature works
        result = await executor("rutracker_search", {"query": "Dune"})
        assert isinstance(result, str)
