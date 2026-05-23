"""Tests for /background gateway slash command.

Tests the _handle_background_command handler (run a prompt in a separate
background session) across gateway messenger platforms.
"""

import asyncio
import os
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


def _make_event(text="/background", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner with minimal mocks."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._background_tasks = set()
    runner._background_reply_continuations = OrderedDict()

    mock_store = MagicMock()
    runner.session_store = mock_store

    from gateway.hooks import HookRegistry
    runner.hooks = HookRegistry()

    return runner


def _configure_background_agent_runtime(runner, monkeypatch, *, final_response="done"):
    """Configure a bare runner so _run_background_task executes inline."""
    from gateway import run as gateway_run

    runner._resolve_session_agent_runtime = MagicMock(
        return_value=("test-model", {"api_key": "test-key"})
    )
    runner._resolve_session_reasoning_config = MagicMock(return_value=None)
    runner._load_service_tier = MagicMock(return_value=None)
    runner._resolve_turn_agent_config = MagicMock(
        return_value={
            "model": "test-model",
            "runtime": {"api_key": "test-key"},
            "request_overrides": None,
        }
    )

    async def run_inline(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = AsyncMock(side_effect=run_inline)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

    mock_adapter = AsyncMock()
    mock_adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="999"))
    mock_adapter.extract_media = MagicMock(return_value=([], final_response))
    mock_adapter.extract_images = MagicMock(return_value=([], final_response))
    runner.adapters[Platform.TELEGRAM] = mock_adapter
    return mock_adapter


# ---------------------------------------------------------------------------
# _handle_background_command
# ---------------------------------------------------------------------------


class TestHandleBackgroundCommand:
    """Tests for GatewayRunner._handle_background_command."""

    @pytest.mark.asyncio
    async def test_no_prompt_shows_usage(self):
        """Running /background with no prompt shows usage."""
        runner = _make_runner()
        event = _make_event(text="/background")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result
        assert "/background" in result

    @pytest.mark.asyncio
    async def test_bg_alias_no_prompt_shows_usage(self):
        """Running /bg with no prompt shows usage."""
        runner = _make_runner()
        event = _make_event(text="/bg")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_empty_prompt_shows_usage(self):
        """Running /background with only whitespace shows usage."""
        runner = _make_runner()
        event = _make_event(text="/background   ")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_valid_prompt_starts_task(self):
        """Running /background with a prompt returns confirmation and starts task."""
        runner = _make_runner()

        # Patch asyncio.create_task to capture the coroutine
        created_tasks = []
        original_create_task = asyncio.create_task

        def capture_task(coro, *args, **kwargs):
            # Close the coroutine to avoid warnings
            coro.close()
            mock_task = MagicMock()
            created_tasks.append(mock_task)
            return mock_task

        with patch("gateway.run.asyncio.create_task", side_effect=capture_task):
            event = _make_event(text="/background Summarize the top HN stories")
            result = await runner._handle_background_command(event)

        assert "🔄" in result
        assert "Background task started" in result
        assert "bg_" in result  # task ID starts with bg_
        assert "Summarize the top HN stories" in result
        assert len(created_tasks) == 1  # background task was created

    @pytest.mark.asyncio
    async def test_telegram_dm_topic_passes_trigger_anchor_to_task(self):
        """Telegram private-topic completion sends need the original command message id."""
        runner = _make_runner()
        runner._run_background_task = AsyncMock()

        def capture_task(coro, *args, **kwargs):
            coro.close()
            mock_task = MagicMock()
            return mock_task

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="20197",
        )
        event = MessageEvent(
            text="/background summarize",
            source=source,
            message_id="463",
            reply_to_message_id="462",
        )

        with patch("gateway.run.asyncio.create_task", side_effect=capture_task):
            result = await runner._handle_background_command(event)

        assert "Background task started" in result
        runner._run_background_task.assert_called_once()
        assert runner._run_background_task.call_args.kwargs["event_message_id"] == "463"

    @pytest.mark.asyncio
    async def test_passes_attached_media_to_background_task(self):
        """Background commands preserve attached image metadata for the spawned task."""
        runner = _make_runner()
        runner._run_background_task = AsyncMock()

        def capture_task(coro, *args, **kwargs):
            coro.close()
            return MagicMock()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
        )
        event = MessageEvent(
            text="/bg describe this",
            message_type=MessageType.PHOTO,
            source=source,
            message_id="463",
            media_urls=["/tmp/a.png"],
            media_types=["image/png"],
        )

        with patch("gateway.run.asyncio.create_task", side_effect=capture_task):
            result = await runner._handle_background_command(event)

        assert "Background task started" in result
        runner._run_background_task.assert_called_once()
        kwargs = runner._run_background_task.call_args.kwargs
        assert kwargs["event_message_id"] == "463"
        assert kwargs["media_urls"] == ["/tmp/a.png"]
        assert kwargs["media_types"] == ["image/png"]
        assert kwargs["message_type"] == MessageType.PHOTO

    @pytest.mark.asyncio
    async def test_prompt_truncated_in_preview(self):
        """Long prompts are truncated to 60 chars in the confirmation message."""
        runner = _make_runner()
        long_prompt = "A" * 100

        with patch("gateway.run.asyncio.create_task", side_effect=lambda c, **kw: (c.close(), MagicMock())[1]):
            event = _make_event(text=f"/background {long_prompt}")
            result = await runner._handle_background_command(event)

        assert "..." in result
        # Should not contain the full prompt
        assert long_prompt not in result

    @pytest.mark.asyncio
    async def test_task_id_is_unique(self):
        """Each background task gets a unique task ID."""
        runner = _make_runner()
        task_ids = set()

        with patch("gateway.run.asyncio.create_task", side_effect=lambda c, **kw: (c.close(), MagicMock())[1]):
            for i in range(5):
                event = _make_event(text=f"/background task {i}")
                result = await runner._handle_background_command(event)
                # Extract task ID from result (format: "Task ID: bg_HHMMSS_hex")
                for line in result.split("\n"):
                    if "Task ID:" in line:
                        tid = line.split("Task ID:")[1].strip()
                        task_ids.add(tid)

        assert len(task_ids) == 5  # all unique

    @pytest.mark.asyncio
    async def test_works_across_platforms(self):
        """The /background command works for all platforms."""
        for platform in [Platform.TELEGRAM, Platform.DISCORD, Platform.SLACK]:
            runner = _make_runner()
            with patch("gateway.run.asyncio.create_task", side_effect=lambda c, **kw: (c.close(), MagicMock())[1]):
                event = _make_event(
                    text="/background test task",
                    platform=platform,
                )
                result = await runner._handle_background_command(event)
                assert "Background task started" in result


# ---------------------------------------------------------------------------
# _run_background_task
# ---------------------------------------------------------------------------


class TestRunBackgroundTask:
    """Tests for GatewayRunner._run_background_task (the actual execution)."""

    @pytest.mark.asyncio
    async def test_no_adapter_returns_silently(self):
        """When no adapter is available, the task returns without error."""
        runner = _make_runner()
        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )
        # No adapters set — should not raise
        await runner._run_background_task("test prompt", source, "bg_test")

    @pytest.mark.asyncio
    async def test_no_credentials_sends_error(self):
        """When provider credentials are missing, an error is sent."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}):
            await runner._run_background_task("test prompt", source, "bg_test")

        # Should have sent an error message
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert "failed" in call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "").lower()

    @pytest.mark.asyncio
    async def test_successful_task_sends_result(self):
        """When the agent completes successfully, the result is sent."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "Hello from background!"))
        mock_adapter.extract_images = MagicMock(return_value=([], "Hello from background!"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        mock_result = {"final_response": "Hello from background!", "messages": []}

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = mock_result
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task("say hello", source, "bg_test")

        # Should have sent the result
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        content = call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "")
        assert "Background task complete" in content
        assert "Hello from background!" in content
        mock_agent_instance.shutdown_memory_provider.assert_called_once()
        mock_agent_instance.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_continuation_loads_existing_background_history(self, monkeypatch):
        """Replies routed into a background task should continue that task's DB history."""
        runner = _make_runner()
        _configure_background_agent_runtime(runner, monkeypatch)
        history = [
            {"role": "user", "content": "first prompt"},
            {"role": "assistant", "content": "first answer"},
        ]
        runner._session_db = MagicMock()
        runner._session_db.get_messages_as_conversation.return_value = history

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = {"final_response": "done", "messages": []}
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task("continue", source, "bg_test")

        runner._session_db.get_messages_as_conversation.assert_called_once_with(
            "bg_test",
            include_ancestors=True,
        )
        mock_agent_instance.run_conversation.assert_called_once_with(
            user_message="continue",
            task_id="bg_test",
            conversation_history=history,
        )

    @pytest.mark.asyncio
    async def test_text_mode_enriches_image_prompt(self, monkeypatch):
        """Text-mode background image handling pre-analyzes image attachments only."""
        runner = _make_runner()
        _configure_background_agent_runtime(runner, monkeypatch)
        runner._decide_image_input_mode = MagicMock(return_value="text")
        runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED PROMPT")

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = {"final_response": "done", "messages": []}
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task(
                "describe it",
                source,
                "bg_test",
                media_urls=["/tmp/a.png", "/tmp/audio.ogg", "/tmp/loose-image"],
                media_types=["image/png", "audio/ogg", "image"],
            )

        runner._enrich_message_with_vision.assert_awaited_once_with(
            "describe it",
            ["/tmp/a.png", "/tmp/loose-image"],
        )
        mock_agent_instance.run_conversation.assert_called_once_with(
            user_message="ENRICHED PROMPT",
            task_id="bg_test",
        )

    @pytest.mark.asyncio
    async def test_native_mode_attaches_image_content_parts(self, monkeypatch, tmp_path):
        """Native-mode background image handling passes multimodal content parts."""
        runner = _make_runner()
        _configure_background_agent_runtime(runner, monkeypatch)
        runner._decide_image_input_mode = MagicMock(return_value="native")
        runner._enrich_message_with_vision = AsyncMock()

        image_path = tmp_path / "one.png"
        image_path.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
            b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
            b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = {"final_response": "done", "messages": []}
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task(
                "what is in this image?",
                source,
                "bg_test",
                media_urls=[str(image_path)],
                media_types=["image/png"],
            )

        runner._enrich_message_with_vision.assert_not_called()
        user_message = mock_agent_instance.run_conversation.call_args.kwargs["user_message"]
        assert isinstance(user_message, list)
        assert user_message[0]["type"] == "text"
        assert "what is in this image?" in user_message[0]["text"]
        assert any(part.get("type") == "image_url" for part in user_message)

    @pytest.mark.asyncio
    async def test_photo_message_with_missing_media_type_is_image(self, monkeypatch):
        """Photo messages without MIME metadata still route attached files as images."""
        runner = _make_runner()
        _configure_background_agent_runtime(runner, monkeypatch)
        runner._decide_image_input_mode = MagicMock(return_value="text")
        runner._enrich_message_with_vision = AsyncMock(return_value="PHOTO ENRICHED")

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = {"final_response": "done", "messages": []}
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task(
                "caption",
                source,
                "bg_test",
                media_urls=["/tmp/photo.jpg"],
                media_types=[],
                message_type=MessageType.PHOTO,
            )

        runner._enrich_message_with_vision.assert_awaited_once_with(
            "caption",
            ["/tmp/photo.jpg"],
        )
        mock_agent_instance.run_conversation.assert_called_once_with(
            user_message="PHOTO ENRICHED",
            task_id="bg_test",
        )

    @pytest.mark.asyncio
    async def test_non_image_attachments_are_not_sent_to_vision(self, monkeypatch):
        """Background audio/doc attachments should not be treated as images."""
        runner = _make_runner()
        _configure_background_agent_runtime(runner, monkeypatch)
        runner._decide_image_input_mode = MagicMock(return_value="text")
        runner._enrich_message_with_vision = AsyncMock()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = {"final_response": "done", "messages": []}
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task(
                "summarize attachments",
                source,
                "bg_test",
                media_urls=["/tmp/audio.ogg", "/tmp/doc.pdf"],
                media_types=["audio/ogg", "application/pdf"],
                message_type=MessageType.DOCUMENT,
            )

        runner._enrich_message_with_vision.assert_not_called()
        mock_agent_instance.run_conversation.assert_called_once_with(
            user_message="summarize attachments",
            task_id="bg_test",
        )

    @pytest.mark.asyncio
    async def test_telegram_dm_topic_completion_preserves_reply_anchor_metadata(self, monkeypatch):
        """Background completion metadata must let Telegram send thread id plus reply id."""
        from gateway import run as gateway_run

        runner = _make_runner()
        runner._resolve_session_agent_runtime = MagicMock(
            return_value=("test-model", {"api_key": "test-key"})
        )
        runner._resolve_session_reasoning_config = MagicMock(return_value=None)
        runner._load_service_tier = MagicMock(return_value=None)
        runner._resolve_turn_agent_config = MagicMock(
            return_value={
                "model": "test-model",
                "runtime": {"api_key": "test-key"},
                "request_overrides": None,
            }
        )
        runner._run_in_executor_with_context = AsyncMock(
            return_value={"final_response": "done", "messages": []}
        )
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "done"))
        mock_adapter.extract_images = MagicMock(return_value=([], "done"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="20197",
        )

        await runner._run_background_task(
            "say hello",
            source,
            "bg_test",
            event_message_id="463",
        )

        mock_adapter.send.assert_called_once()
        assert mock_adapter.send.call_args.kwargs["metadata"] == {
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "20197",
            "telegram_reply_to_message_id": "463",
        }

    @pytest.mark.asyncio
    async def test_successful_task_records_all_completion_message_ids_for_replies(self, monkeypatch):
        """Every visible completion message chunk should become a reply continuation anchor."""
        from gateway import run as gateway_run

        runner = _make_runner()
        runner._resolve_session_agent_runtime = MagicMock(
            return_value=("test-model", {"api_key": "test-key"})
        )
        runner._resolve_session_reasoning_config = MagicMock(return_value=None)
        runner._load_service_tier = MagicMock(return_value=None)
        runner._resolve_turn_agent_config = MagicMock(
            return_value={
                "model": "test-model",
                "runtime": {"api_key": "test-key"},
                "request_overrides": None,
            }
        )
        runner._run_in_executor_with_context = AsyncMock(
            return_value={"final_response": "done", "messages": []}
        )
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock(
            return_value=SendResult(
                success=True,
                message_id="999",
                continuation_message_ids=("1000",),
                raw_response={"message_ids": ["1001"]},
            )
        )
        mock_adapter.extract_media = MagicMock(return_value=([], "done"))
        mock_adapter.extract_images = MagicMock(return_value=([], "done"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="20197",
        )

        await runner._run_background_task(
            "say hello",
            source,
            "bg_test",
            event_message_id="463",
        )

        for message_id in ("999", "1000", "1001"):
            entry = runner._background_reply_continuations[("telegram", "67890", message_id)]
            assert entry["task_id"] == "bg_test"
            assert entry["source"].thread_id == "20197"

    def test_completion_reply_anchor_map_is_bounded(self):
        """The in-memory reply continuation map should evict oldest anchors."""
        runner = _make_runner()
        runner._background_reply_continuations_max = 2
        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="20197",
        )

        for message_id in ("1", "2", "3"):
            runner._remember_background_reply_continuation(
                source=source,
                task_id="bg_test",
                send_result=SendResult(success=True, message_id=message_id),
            )

        assert list(runner._background_reply_continuations) == [
            ("telegram", "67890", "2"),
            ("telegram", "67890", "3"),
        ]

    @pytest.mark.asyncio
    async def test_agent_cleanup_runs_when_background_agent_raises(self):
        """Temporary background agents must be cleaned up on error paths too."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.side_effect = RuntimeError("boom")
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task("say hello", source, "bg_test")

        mock_adapter.send.assert_called_once()
        mock_agent_instance.shutdown_memory_provider.assert_called_once()
        mock_agent_instance.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_sends_error_message(self):
        """When the agent raises an exception, an error message is sent."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=RuntimeError("boom")):
            await runner._run_background_task("test prompt", source, "bg_test")

        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        content = call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "")
        assert "failed" in content.lower()


class TestBackgroundReplyContinuation:
    """Tests for replies to background completion messages."""

    @pytest.mark.asyncio
    async def test_reply_to_known_completion_routes_to_same_background_session(self):
        """Known completion replies should start the same bg session before normal dispatch."""
        runner = _make_runner()
        runner._is_user_authorized = MagicMock(return_value=True)
        runner._session_key_for_source = MagicMock(side_effect=AssertionError("normal session dispatch should not run"))
        runner._run_background_task = AsyncMock()

        stored_source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="20197",
        )
        event_source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
        )
        runner._background_reply_continuations[("telegram", "67890", "999")] = {
            "task_id": "bg_seeded",
            "source": stored_source,
        }
        event = MessageEvent(
            text="continue this",
            source=event_source,
            message_id="1002",
            reply_to_message_id="999",
            message_type=MessageType.PHOTO,
            media_urls=["/tmp/reply-image.png"],
            media_types=["image/png"],
        )

        with patch("gateway.run.asyncio.create_task", side_effect=lambda c, **kw: (c.close(), MagicMock())[1]):
            result = await runner._handle_message(event)

        assert "Background task started" in result
        assert "bg_seeded" in result
        runner._run_background_task.assert_called_once()
        assert runner._run_background_task.call_args.args[2] == "bg_seeded"
        assert runner._run_background_task.call_args.args[1].thread_id == "20197"
        assert runner._run_background_task.call_args.kwargs["event_message_id"] == "1002"
        assert runner._run_background_task.call_args.kwargs["media_urls"] == ["/tmp/reply-image.png"]
        assert runner._run_background_task.call_args.kwargs["media_types"] == ["image/png"]
        assert runner._run_background_task.call_args.kwargs["message_type"] is MessageType.PHOTO
        runner._session_key_for_source.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_to_unknown_completion_falls_through_to_normal_dispatch(self):
        """Replies that do not match the bounded bg map keep normal chat routing."""
        runner = _make_runner()
        runner.config = {}
        runner._draining = False
        runner._is_user_authorized = MagicMock(return_value=True)
        runner._session_key_for_source = MagicMock(return_value="telegram:67890:12345")
        runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
        runner._begin_session_run_generation = MagicMock(return_value=7)
        runner._handle_message_with_agent = AsyncMock(return_value="normal dispatch")
        runner._post_turn_goal_continuation = AsyncMock()
        runner._release_running_agent_state = MagicMock()
        runner._run_background_task = AsyncMock()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="20197",
        )
        runner._background_reply_continuations[("telegram", "67890", "999")] = {
            "task_id": "bg_seeded",
            "source": source,
        }
        event = MessageEvent(
            text="regular reply",
            source=source,
            message_id="1002",
            reply_to_message_id="404",
        )

        result = await runner._handle_message(event)

        assert result == "normal dispatch"
        runner._run_background_task.assert_not_called()
        runner._handle_message_with_agent.assert_awaited_once()


# ---------------------------------------------------------------------------
# /background in help and known_commands
# ---------------------------------------------------------------------------


class TestBackgroundInHelp:
    """Verify /background appears in help text and known commands."""

    @pytest.mark.asyncio
    async def test_background_in_help_output(self):
        """The /help output includes /background."""
        runner = _make_runner()
        event = _make_event(text="/help")
        result = await runner._handle_help_command(event)
        assert "/background" in result

    def test_background_is_known_command(self):
        """The /background command is in GATEWAY_KNOWN_COMMANDS."""
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        assert "background" in GATEWAY_KNOWN_COMMANDS

    def test_bg_alias_is_known_command(self):
        """The /bg alias is in GATEWAY_KNOWN_COMMANDS."""
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        assert "bg" in GATEWAY_KNOWN_COMMANDS


# ---------------------------------------------------------------------------
# CLI /background command definition
# ---------------------------------------------------------------------------


class TestBackgroundInCLICommands:
    """Verify /background is registered in the CLI command system."""

    def test_background_in_commands_dict(self):
        """The /background command is in the COMMANDS dict."""
        from hermes_cli.commands import COMMANDS
        assert "/background" in COMMANDS

    def test_bg_alias_in_commands_dict(self):
        """The /bg alias is in the COMMANDS dict."""
        from hermes_cli.commands import COMMANDS
        assert "/bg" in COMMANDS

    def test_background_in_session_category(self):
        """The /background command is in the Session category."""
        from hermes_cli.commands import COMMANDS_BY_CATEGORY
        assert "/background" in COMMANDS_BY_CATEGORY["Session"]

    def test_background_autocompletes(self):
        """The /background command appears in autocomplete results."""
        pytest.importorskip("prompt_toolkit")
        from hermes_cli.commands import SlashCommandCompleter
        from prompt_toolkit.document import Document

        completer = SlashCommandCompleter()
        doc = Document("backgro")  # Partial match
        completions = list(completer.get_completions(doc, None))
        # Text doesn't start with / so no completions
        assert len(completions) == 0

        doc = Document("/backgro")  # With slash prefix
        completions = list(completer.get_completions(doc, None))
        cmd_displays = [str(c.display) for c in completions]
        assert any("/background" in d for d in cmd_displays)
