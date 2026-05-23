# /background image attachment support spec

Date: 2026-05-21
Scope: implementation note; core implementation lives in `gateway/run.py` with focused coverage in `tests/gateway/test_background_command.py`.

## Objective

Allow a gateway command like `/bg analyze this` or `/background analyze this` to use an attached image in the spawned background AIAgent session, especially for Telegram photo captions/albums. Reply-continuation follow-ups should also preserve attached media when they continue an existing background task.

## Current data path

### Shared contract

- `gateway/platforms/base.py`
  - `MessageType` is defined around line 919.
  - `MessageEvent` is defined around line 941.
  - `MessageEvent.media_urls` and `MessageEvent.media_types` are the cross-platform carrier for local cached attachments. The inline comment says `media_urls` are local file paths for vision tool access.
  - `MessageEvent.merge()` around lines 1139-1152 extends `media_urls` and `media_types` when batched events are merged.

### Telegram

- `gateway/platforms/telegram.py`
  - `_handle_media_message()` builds a `MessageEvent` via `_build_message_event()` around line 4997, then puts the caption into `event.text` around lines 4999-5001.
  - Regular photos are downloaded to the image cache and assigned to `event.media_urls` / `event.media_types` around lines 5014-5041.
  - Telegram image documents are normalized into `MessageType.PHOTO` and image MIME types around lines 5119-5148.
  - Telegram albums/media groups are debounced and merged in `_queue_media_group_event()` / `_flush_media_group_event()` around lines 5228-5262.
  - Non-album photo bursts use `_enqueue_photo_event()` / `_flush_photo_batch()` around lines 4926-4955.

Implication: Telegram already gives the `/background` command handler a single `MessageEvent` with the command text in `event.text` and all cached images in `event.media_urls`, as long as the user attaches the image(s) to the command message/caption.

### Discord and other platforms

- `gateway/platforms/discord.py`
  - Image attachments are downloaded/cached around lines 4637-4658.
  - The created `MessageEvent` carries `media_urls` and `media_types` around lines 4812-4826.
- Similar `media_urls`/`media_types` plumbing exists in BlueBubbles, DingTalk, Email, Feishu, Matrix, Mattermost, Signal, Slack, WeCom, Weixin, WhatsApp, and Yuanbao adapters.

Design can therefore be generic: consume `MessageEvent.media_urls` and `media_types` in the gateway `/background` command path, not Telegram-specific update objects.

## Current normal-image agent path

- `gateway/run.py::_prepare_inbound_message_text()` starts around line 7556.
  - It collects image paths when `mtype.startswith("image/")` or `event.message_type == MessageType.PHOTO` around lines 7606-7612.
  - It chooses native-vs-text image routing with `_decide_image_input_mode()` around lines 7623-7647.
  - Text mode calls `_enrich_message_with_vision()` and injects descriptions into the user text.
  - Native mode stores image paths in a session-scoped pending buffer so the `_run_agent()` call site can build OpenAI-style multimodal content parts.
- `gateway/run.py::_run_agent()` consumes that pending native-image buffer and calls `agent.image_routing.build_native_content_parts()` around lines 16925-16956.
- `agent/image_routing.py` contains the canonical decision/build helpers:
  - `decide_image_input_mode()` around lines 189-219.
  - `build_native_content_parts()` around lines 320-385.

## Current `/background` path

- `gateway/run.py::_handle_background_command()` starts around line 11324.
  - It parses the prompt from `event.get_command_args()`.
  - It currently copies `event.media_urls` and `event.media_types` into the background task call around lines 11340-11353.
- `gateway/run.py::_run_background_task()` starts around line 11361.
  - It currently accepts `media_urls` and `media_types` around lines 11366-11374.
  - It filters `media_urls` for `mtype.startswith("image/")` and calls `_enrich_message_with_vision()` around lines 11411-11427.
  - It then passes only `enriched_prompt` as a string to `agent.run_conversation()` around lines 11457-11461.

So current code already preserves image attachments for `/background` in text-vision mode. It does not yet fully mirror the normal inbound image path because it does not use native multimodal routing and it only recognizes `image/*` media types, not every platform's looser image marker.

## Implementation shape

Keep the change inside `gateway/run.py` plus tests. Do not touch Telegram adapter internals unless tests prove the event is not constructed correctly.

1. Add a tiny helper near the existing media helpers in `gateway/run.py`, for example:

   - `_image_paths_from_media(media_urls: list[str], media_types: list[str], *, message_type: MessageType | None = None) -> list[str]`

   Behavior:
   - Keep paths where media type starts with `image/`.
   - Also accept exact/loose `image` media markers for adapters like DingTalk that currently store `media_types.append("image")`.
   - If `message_type == MessageType.PHOTO`, keep paths even when the media type is missing or non-standard.
   - Do not treat documents/audio/video as images.

2. Pass `event.message_type` from `_handle_background_command()` to `_run_background_task()` or pass pre-filtered `image_paths` directly.

   Safer shape:
   - `_handle_background_command()` captures copied `media_urls`, `media_types`, and `message_type=event.message_type`.
   - `_run_background_task(..., media_urls=None, media_types=None, message_type=None)` computes `image_paths` using the helper.

3. In `_run_background_task()`, mirror normal routing:

   - Compute `image_paths` once.
   - If no image paths, keep current string prompt path.
   - If image paths exist:
     - Call `self._decide_image_input_mode()`.
     - If mode is `text`, keep current `_enrich_message_with_vision(prompt, image_paths)` path.
     - If mode is `native`, call `agent.image_routing.build_native_content_parts(prompt, image_paths)` inside or just before `run_sync()` and pass the resulting content list to `agent.run_conversation(user_message=...)`.
     - If native attachment fails or all images are skipped, fall back to text enrichment or at least the plain prompt with a warning log. The normal path falls back to text/plain on native build exceptions; background should not fail the whole task because of an unreadable attachment.

   Implementation detail:
   - Avoid using `_prepare_inbound_message_text()` for background tasks. Its native-image buffer is keyed by the foreground gateway session key, not by `task_id`; using it from a background task risks interfering with the active foreground session.

4. Keep delivery/thread behavior stable:

   - Preserve `event_message_id = self._reply_anchor_for_event(event)` in `_handle_background_command()`.
   - Preserve `_thread_metadata_for_source(source, event_message_id)` in `_run_background_task()`.
   - When reply-continuation maps are involved, copy message text plus media metadata into the continued background task without changing the original reply anchor semantics.

5. Optional, not required for this minimal feature:

   - Add a warning message to the background prompt for unsupported non-image attachments. Existing foreground document/audio enrichment is richer, but this card is specifically image support.
   - If later expanding to voice/audio/doc background support, refactor a pure helper out of `_prepare_inbound_message_text()` instead of duplicating its side effects.

## Tests

The focused gateway test file should cover these cases:

1. `test_background_command_passes_attached_media_to_background_task`

   Purpose: command handler preserves `MessageEvent.media_urls` / `media_types`.

   Shape:
   - Build runner with `_make_runner()`.
   - Patch `gateway.run.asyncio.create_task` to close the coroutine and return a mock task with `add_done_callback`.
   - Create `MessageEvent(text="/bg describe", message_type=MessageType.PHOTO, media_urls=["/tmp/a.png"], media_types=["image/png"], source=source, message_id="463")`.
   - Call `await runner._handle_background_command(event)`.
   - Assert `_run_background_task` was scheduled with `media_urls=["/tmp/a.png"]`, `media_types=["image/png"]`, and the original reply anchor.

2. `test_run_background_task_text_mode_enriches_image_prompt`

   Purpose: text-mode background path uses the same vision enrichment behavior.

   Shape:
   - Mock `_decide_image_input_mode` to return `"text"`.
   - Mock `_enrich_message_with_vision = AsyncMock(return_value="ENRICHED PROMPT")`.
   - Mock `_run_in_executor_with_context` to execute the sync function or capture its closure so that `agent.run_conversation()` can be inspected.
   - Patch `run_agent.AIAgent` and assert `run_conversation(user_message="ENRICHED PROMPT", task_id="bg_test")`.
   - Include one non-image media item and assert only image paths are sent to `_enrich_message_with_vision`.

3. `test_run_background_task_native_mode_attaches_image_content_parts`

   Purpose: native vision-capable models receive pixels, not only text summaries.

   Shape:
   - Create a real tiny PNG/JPEG in `tmp_path`.
   - Mock `_decide_image_input_mode` to return `"native"`.
   - Patch `agent.image_routing.build_native_content_parts` or let it run against the real tiny image.
   - Patch `run_agent.AIAgent` and execute the sync function.
   - Assert `run_conversation()` receives a list whose first element has `{"type": "text"}` and at least one later element has `{"type": "image_url"}`.
   - Assert `_enrich_message_with_vision` was not called.

4. `test_run_background_task_treats_photo_message_with_missing_media_type_as_image`

   Purpose: Telegram/DingTalk-ish loose metadata still works.

   Shape:
   - Use `message_type=MessageType.PHOTO`, `media_urls=["/tmp/a.jpg"]`, `media_types=[""]` or `media_types=[]`.
   - Assert image path is included in enrichment/native build path.

5. `test_run_background_task_ignores_non_image_attachments_for_vision`

   Purpose: audio/doc attachments do not get sent to vision by accident.

   Shape:
   - `media_urls=["/tmp/audio.ogg", "/tmp/doc.pdf"]`, `media_types=["audio/ogg", "application/pdf"]`.
   - Assert `_enrich_message_with_vision` is not called and `agent.run_conversation()` receives the original prompt.

6. Telegram adapter regression (only if desired, not required for gateway implementation): `tests/gateway/test_telegram_media_batching.py::test_album_caption_background_event_preserves_all_media_urls`

   Purpose: a Telegram album with caption `/bg compare these` flushes as one `MessageEvent` with command text and all `media_urls`.

   This can be a pure adapter unit test around `_queue_media_group_event()` / `_flush_media_group_event()` with fake `MessageEvent`s. It should not hit the Telegram API.

## Known constraints / non-goals

- If the user sends an image as one message and then sends `/bg describe that image` as a separate later message, there is no current association mechanism. This spec only covers images attached to the `/bg` command message/caption, media batched into the same logical event, or media attached to an explicit background reply-continuation.
- The original gap was full parity with native multimodal image routing and looser platform MIME markers; implementation should preserve the existing text-mode vision path as fallback.
- The final implementation intentionally touches both initial `/background` image support and reply-continuation media forwarding because they share the same background task media path.

## Verification performed

- Inspected `gateway/platforms/base.py`, `gateway/platforms/telegram.py`, `gateway/platforms/discord.py`, `gateway/run.py`, `agent/image_routing.py`, and `tests/gateway/test_background_command.py`.
- Current focused verification: `venv/bin/python -m pytest -q tests/gateway/test_background_command.py -o 'addopts='`.
  - Result: 30 passed.
