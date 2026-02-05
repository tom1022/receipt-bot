import discord
import asyncio
import json
import time
from io import BytesIO
from PIL import Image
from config import DISCORD_TOKEN, TARGET_CHANNEL_ID
from llm_utils import analyze_receipt_with_ollama
from sheets_utils import append_receipt_row

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

WORKER_CONCURRENCY = 2

# Queue for serializing image processing. Initialized on_ready to bind to running loop.
image_queue = None
# pending reviews: message.id -> {'parsed_result': ..., 'processed': False}
pending_reviews = {}


def resize_image_to_max_pixels(image_bytes, max_pixels=1024):
    """
    リサイズして、最大ピクセルが max_pixels になるようにする。
    アスペクト比を保持します。
    """
    try:
        img = Image.open(BytesIO(image_bytes))
        # 最大の辺を max_pixels にリサイズ
        img.thumbnail((max_pixels, max_pixels), Image.Resampling.LANCZOS)
        # バイト列に変換
        output = BytesIO()
        img.save(output, format='PNG')
        return output.getvalue()
    except Exception as e:
        print(f"Image resize failed: {e}")
        return image_bytes


def _is_valid_result(parsed_result):
    """Return (True, []) if valid, else (False, list_of_missing_or_errors)."""
    missing = []
    if not isinstance(parsed_result, dict):
        return False, ['invalid_json']
    # required keys
    for k in ('store', 'date', 'total_amount', 'category'):
        v = parsed_result.get(k)
        if v is None or (isinstance(v, str) and v.strip() == ''):
            missing.append(k)

    # check total_amount numeric-ish
    ta = parsed_result.get('total_amount')
    if ta is not None:
        try:
            # allow strings like "1,234" or numeric
            if isinstance(ta, str):
                _ = float(str(ta).replace(',', ''))
            else:
                _ = float(ta)
        except Exception:
            missing.append('total_amount_not_numeric')

    return (len(missing) == 0), missing


async def queue_worker():
    global image_queue
    if image_queue is None:
        image_queue = asyncio.Queue()

    while True:
        status_msg = None
        item = await image_queue.get()
        if item is None:
            image_queue.task_done()
            break

        # item can be either (message, attachment) for normal processing
        # or (message, image_bytes, is_reanalysis, orig_sent_id) for reanalysis
        is_reanalysis = False
        orig_sent_id = None
        image_data = None
        attachment = None
        if isinstance(item, tuple) and len(item) == 2:
            message, attachment = item
        elif isinstance(item, tuple) and len(item) == 3:
            # (message, attachment, status_msg)
            message, attachment, status_msg = item
        elif isinstance(item, tuple) and len(item) == 4:
            message, attachment_or_bytes, is_reanalysis, orig_sent_id = item
            if isinstance(attachment_or_bytes, (bytes, bytearray)):
                image_data = attachment_or_bytes
            else:
                attachment = attachment_or_bytes
        else:
            # fallback
            try:
                message, attachment = item
            except Exception:
                # malformed queue item
                image_queue.task_done()
                continue

        # ensure we have a status_msg to edit/reply to
        if status_msg is None:
            status_msg = await message.channel.send("画像解析中")
        else:
            try:
                await status_msg.edit(content=f"画像解析中 (キュー残り: {image_queue.qsize()})")
            except Exception:
                pass
        try:
            if image_data is None:
                # read from attachment if not provided
                image_data = await attachment.read()
            
            # リサイズして最大ピクセルを1024にする
            image_data = await asyncio.to_thread(resize_image_to_max_pixels, image_data, 1024)

            await status_msg.edit(content="LLM解析中")

            json_result = await asyncio.to_thread(analyze_receipt_with_ollama, image_data)

            # Always reply to the status message so JSON is clearly linked to the queued image
            try:
                now_ts = int(time.time())
                timeout_seconds = 180
                auto_add_at = now_ts + timeout_seconds
                reply_content = (
                    f"Parsed JSON — 受信: <t:{now_ts}:R>\n自動追加予定: <t:{auto_add_at}:R> (このメッセージに反応が無ければ自動的に追加されます)\n\n````json\n{json_result}\n```"
                )
                try:
                    sent = await status_msg.reply(reply_content)
                except Exception:
                    sent = await message.channel.send(reply_content)
            except Exception:
                # fallback: send normally without timestamps
                sent = await status_msg.reply(f"Parsed JSON:\n```json\n{json_result}\n```")

            # try to decode the JSON result into a dict for sheet-friendly fields
            try:
                parsed_result = json.loads(json_result) if isinstance(json_result, str) else json_result
            except Exception:
                parsed_result = {'raw': json_result}

            # validate; if invalid, attempt one automatic reanalysis
            valid, missing = _is_valid_result(parsed_result)
            if not valid:
                try:
                    await message.channel.send("解析結果が不完全なため自動で再解析を試みます…")
                    json_result2 = await asyncio.to_thread(analyze_receipt_with_ollama, image_data)
                    try:
                        parsed_result2 = json.loads(json_result2) if isinstance(json_result2, str) else json_result2
                    except Exception:
                        parsed_result2 = {'raw': json_result2}

                    valid2, missing2 = _is_valid_result(parsed_result2)
                    if valid2:
                        parsed_result = parsed_result2
                        # update the bot message with the improved JSON
                        try:
                            await sent.edit(content=f"Parsed JSON:\n```json\n{json.dumps(parsed_result, ensure_ascii=False, indent=2)}\n```")
                        except Exception:
                            pass
                    else:
                        # automatic retry failed; notify and add manual re-run option
                        try:
                            await message.channel.send(f"自動再解析でも不完全でした (欠落: {missing2}). 手動で再解析するには❓を押してください。")
                        except Exception:
                            pass
                        try:
                            await sent.add_reaction('❓')
                        except Exception:
                            pass
                except Exception:
                    try:
                        await message.channel.send("自動再解析でエラーが発生しました。❓で手動再解析してください。")
                    except Exception:
                        pass
                    try:
                        await sent.add_reaction('❓')
                    except Exception:
                        pass
            else:
                # valid on first try — still offer manual re-run
                try:
                    await sent.add_reaction('❓')
                except Exception:
                    pass

            # add other reaction options
            try:
                # Always add the standard action reactions to the parsed-result message.
                # Previously the code skipped this for reanalysis (assuming the original message
                # already had them), which led to reanalysis messages only having ❓.
                await sent.add_reaction('✅')
                await sent.add_reaction('❌')
                await sent.add_reaction('⚠️')
            except Exception:
                pass

            # keep the parsed result and source info in memory until a human reacts
            try:
                # For initial processing, key by the new sent message id
                key_id = sent.id
                pending_reviews[key_id] = {
                    'parsed_result': parsed_result,
                    'processed': False,
                    'image_data': image_data,
                    'in_reanalysis': False,
                    'channel_id': message.channel.id,
                    'queue_msg_id': status_msg.id if status_msg is not None else None,
                    'auto_add_at': auto_add_at,
                }
                # schedule auto-flag task: if no reaction in 3 minutes, append with flag
                asyncio.create_task(_auto_flag_after_timeout(key_id, timeout_seconds))
            except Exception:
                pass

            print(f"--- LLM JSON Result ---\n{json_result}\n-----------------------")

            # Do not delete the status message — keep it as an anchor for replies
            try:
                await status_msg.edit(content=f"処理完了 — 結果はこのメッセージへの返信を確認してください。 (キュー残り: {image_queue.qsize()})")
            except Exception:
                pass
        except Exception as e:
            print(f"Error in queue worker: {e}")
            try:
                await message.channel.send(f"エラーが発生しました: {e}")
            except Exception:
                pass
        finally:
            image_queue.task_done()


async def _auto_flag_after_timeout(message_id, delay_seconds=180):
    """If a pending review exists and no reaction within delay, append to Sheets with flag."""
    await asyncio.sleep(delay_seconds)
    try:
        entry = pending_reviews.get(message_id)
        if not entry:
            return
        if entry.get('processed'):
            return

        # mark processed to avoid race with manual reaction
        entry['processed'] = True

        # prepare data (ensure dict)
        parsed = entry.get('parsed_result')
        if not isinstance(parsed, dict):
            parsed = {'raw': parsed}
        parsed['flag_needs_fix'] = True

        # append to google sheets in executor
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, append_receipt_row, parsed)
        except Exception as e:
            # failure is non-fatal; log to channel
            try:
                ch = client.get_channel(entry.get('channel_id'))
                if ch:
                    await ch.send(f'自動追加中にエラーが発生しました: {e}')
            except Exception:
                pass

        # notify channel
        try:
            ch = client.get_channel(entry.get('channel_id'))
            if ch:
                await ch.send('3分間リアクションがなかったため、自動でフラグ付きでGoogle Sheetsに追加しました。')
        except Exception:
            pass

        # cleanup
        try:
            pending_reviews.pop(message_id, None)
        except Exception:
            pass
    except Exception:
        return


@client.event
async def on_ready():
    global image_queue
    print(f'Logged in as {client.user}')
    # create the queue and start worker
    if image_queue is None:
        image_queue = asyncio.Queue()
    # start background workers (parallel processing up to WORKER_CONCURRENCY)
    for _ in range(WORKER_CONCURRENCY):
        client.loop.create_task(queue_worker())


@client.event
async def on_message(message):
    global image_queue
    if message.author == client.user:
        return

    # If a target channel is configured, ignore messages from other channels.
    if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
        return

    if message.attachments:
        imgs = [a for a in message.attachments if any(a.filename.lower().endswith(ext) for ext in ['png', 'jpg', 'jpeg', 'webp', 'heic'])]
        if not imgs:
            return

        # ensure queue exists
        if image_queue is None:
            image_queue = asyncio.Queue()

        for a in imgs:
            pos = image_queue.qsize() + 1
            # create a status message which will act as the anchor for replies
            status_msg = await message.channel.send(f"画像をキューに追加しました (位置: {pos})")
            await image_queue.put((message, a, status_msg))


def run_bot():
    client.run(DISCORD_TOKEN)


@client.event
async def on_reaction_add(reaction, user):
    # Only act on human reactions to bot messages that are pending review
    try:
        if user == client.user:
            return
        message = reaction.message
        if message.author != client.user:
            return
        entry = pending_reviews.get(message.id)
        if not entry:
            return

        emoji = str(reaction.emoji)

        # Handle manual reanalysis (❓) separately; do not mark as final processed
        if emoji == '❓':
            # prevent concurrent reanalysis
            if entry.get('in_reanalysis'):
                return
            # mark original entry as processed to avoid the auto-add timeout
            # (the reanalysis will create a new pending entry for the new message)
            if entry.get('processed'):
                return
            entry['processed'] = True
            entry['in_reanalysis'] = True

            # enqueue reanalysis into the serial queue so it runs through OCR->phone->LLM
            try:
                # ensure queue exists
                global image_queue
                if image_queue is None:
                    image_queue = asyncio.Queue()

                # get image bytes
                image_bytes = entry.get('image_data')
                if not image_bytes:
                    # append log to the parsed JSON message instead of sending separate channel message
                    try:
                        new_content = message.content + f"\n\n- ❓ 再解析に失敗しました: 元画像が見つかりませんでした (実行者: @{user.display_name})"
                        await message.edit(content=new_content)
                    except Exception:
                        pass
                    entry['in_reanalysis'] = False
                    return

                # fetch the original queue status message so we can keep replies anchored
                try:
                    ch = client.get_channel(entry.get('channel_id'))
                    status_msg = None
                    if ch and entry.get('queue_msg_id'):
                        try:
                            status_msg = await ch.fetch_message(entry.get('queue_msg_id'))
                        except Exception:
                            status_msg = None
                except Exception:
                    status_msg = None

                # append a log line to the parsed JSON message indicating reanalysis queued
                try:
                    new_content = message.content + f"\n\n- ❓ 再解析をキューに追加しました (実行者: @{user.display_name})"
                    await message.edit(content=new_content)
                except Exception:
                    pass

                await image_queue.put((status_msg or message, image_bytes, True, message.id))
            except Exception as e:
                entry['in_reanalysis'] = False
                try:
                    new_content = message.content + f"\n\n- ❓ 再解析キュー登録でエラーが発生しました: {e} (実行者: @{user.display_name})"
                    await message.edit(content=new_content)
                except Exception:
                    pass

            return

        # For other emojis, ensure it's not already processed
        if entry.get('processed'):
            return

        # Mark processed to avoid races for final actions
        entry['processed'] = True

        if emoji == '✅':
            # append as-is and update the parsed JSON message with action log
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, append_receipt_row, entry['parsed_result'])
                try:
                    new_content = message.content + f"\n\n- ✅ Google Sheets に追加しました (実行者: @{user.display_name})"
                    await message.edit(content=new_content)
                except Exception:
                    pass
            except Exception as e:
                try:
                    new_content = message.content + f"\n\n- ✅ 追加中にエラーが発生しました: {e} (実行者: @{user.display_name})"
                    await message.edit(content=new_content)
                except Exception:
                    pass

        elif emoji == '❌':
            try:
                new_content = message.content + f"\n\n- ❌ この結果は破棄されました (実行者: @{user.display_name})"
                await message.edit(content=new_content)
            except Exception:
                pass

        elif emoji == '⚠️':
            # flag and append, then update the parsed JSON message with action log
            try:
                data = entry['parsed_result']
                if isinstance(data, dict):
                    data['flag_needs_fix'] = True
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, append_receipt_row, data)
                try:
                    new_content = message.content + f"\n\n- ⚠️ フラグを付けてGoogle Sheetsに追加しました (実行者: @{user.display_name})"
                    await message.edit(content=new_content)
                except Exception:
                    pass
            except Exception as e:
                try:
                    new_content = message.content + f"\n\n- ⚠️ 追加中にエラーが発生しました: {e} (実行者: @{user.display_name})"
                    await message.edit(content=new_content)
                except Exception:
                    pass

        # cleanup
        try:
            pending_reviews.pop(message.id, None)
        except Exception:
            pass
    except Exception:
        return
