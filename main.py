"""Точка входа: webhook-версия для Render Web Service (Free)."""

import os
import re
import json
import asyncio
import html
import time
import logging

import aiohttp
from datetime import time as dtime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    TypeHandler, ContextTypes, filters,
)

import db
import resolver
import pr
import roles
import top
import ship
import wedding
import profile
import economy
import tictactoe
import sss
import complaints
import warns
import aura
import aura_admin
import awards
import actions
import ai
import help_menu
import casino
import polls
import rules
import command_registry   # noqa: F401 — должен идти до commands_dop
import commands_dop       # noqa: F401 — регистрирует команды в реестре
import commands_dop_handlers
import shop
import reputation
import blacklist
import helper


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
CHAT_ID = int(os.getenv("CHAT_ID", "0") or "0")

if not ADMIN_ID:
    logger.error("main: ADMIN_ID не задан — админ-команды работать не будут!")

PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = "webhook"

# Обращения к Ксении
_AI_WORD = re.compile(
    r"(?i)^(ксюш(?:а|и)?|кеся|ксения|ксюха|ксю)(\s|$)"
)


async def _error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    u = update.effective_user
    msg = update.effective_message

    if not msg:
        return

    if u:
        await db.upsert_user(u.id, u.username, u.first_name)

    await msg.reply_text(
        "Привет! Я PR-бот.\n"
        "/help — список команд по ролям\n"
        "/check — подписка\n"
        "/ping — жив?\n"
        "/restart — сбросить контекст ИИ\n"
        "Профиль — профиль\n"
        "Казино — мини-казино\n"
        "Магазин — пакеты команд"
    )


async def cmd_ping(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text("pong 🏓")


async def cmd_delete_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Удаляет сообщение-реплай и команду -смс."""

    msg = update.effective_message
    chat = update.effective_chat

    if not msg or not chat:
        return

    if not await roles._require_role(update, ("commissar",)):
        await msg.reply_text(
            "⛔ Только владелец или комиссар могут использовать -смс."
        )
        return

    target = msg.reply_to_message

    if not target:
        await msg.reply_text(
            "Ответь командой <code>-смс</code> на сообщение, которое нужно удалить.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await context.bot.delete_message(
            chat_id=chat.id,
            message_id=target.message_id,
        )
    except Exception as e:
        logger.warning(
            "cmd_delete_message: не удалось удалить цель: "
            "chat_id=%s message_id=%s: %s",
            chat.id,
            target.message_id,
            e,
        )
        await msg.reply_text(
            "⚠️ Не удалось удалить отмеченное сообщение. "
            "Проверь права бота администратора и возможность удаления этого сообщения."
        )
        return

    try:
        await msg.delete()
    except Exception as e:
        logger.warning(
            "cmd_delete_message: цель удалена, но команда осталась: "
            "chat_id=%s message_id=%s: %s",
            chat.id,
            msg.message_id,
            e,
        )


async def cmd_restart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    msg = update.effective_message

    if not user or not msg:
        return

    ai.reset_history(user.id)
    await msg.reply_text("🔄 Контекст ИИ сброшен.")


def _split_telegram_text(
    text: str,
    limit: int = 3900,
) -> list[str]:
    """Делит длинный ответ на сообщения."""

    text = str(text or "")
    chunks = []

    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)

        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit + 1)

        if cut < limit // 2:
            cut = limit

        chunks.append(text[:cut].strip())
        text = text[cut:].strip()

    if text:
        chunks.append(text)

    return chunks or [""]


def _truncate_utf8(value: str, max_bytes: int) -> str:
    raw = (value or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return value or ""
    return raw[:max_bytes].decode("utf-8", errors="ignore") + "…"


def _markdown_to_html(value: str) -> str:
    """Безопасный небольшой Markdown-конвертер для сообщений Telegram."""
    text = html.escape(value or "")
    text = re.sub(r"```(?:[\w+-]+)?\n?(.*?)```", r"<pre>\1</pre>", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"^###\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"^##\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"^#\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    return text


_WIDGET_HTML = r'''<!doctype html>
<html><head><meta charset="utf-8"><style>
:root{color-scheme:light dark}body{font-family:system-ui,sans-serif;margin:0;color:var(--tg-theme-text-color)}
#answer{white-space:normal;overflow-wrap:anywhere;line-height:1.48;user-select:text}
#answer p{margin:0 0 8px}#answer h1,#answer h2,#answer h3{font-size:1.05em;margin:10px 0 6px}
#answer pre{white-space:pre-wrap;background:var(--tg-theme-secondary-bg-color);padding:8px;border-radius:8px}
#answer code{font-family:monospace;background:var(--tg-theme-secondary-bg-color);border-radius:4px;padding:1px 3px}
#meta{border-top:1px solid var(--tg-theme-section-separator-color);margin-top:12px;padding-top:9px;color:var(--tg-theme-hint-color);font-size:12px}
.stat{display:flex;justify-content:space-between;gap:12px;margin:4px 0}.value{color:var(--tg-theme-text-color);font-variant-numeric:tabular-nums;font-weight:600}
#status{font-size:11px;color:var(--tg-theme-hint-color);margin-bottom:8px;letter-spacing:.04em;text-transform:uppercase}
</style></head><body>
<div id="status">Ксения готовит ответ</div><div id="answer">Загружаю…</div>
<div id="meta"><div class="stat"><span>Потрачено</span><span class="value" id="spent">0</span></div>
<div class="stat"><span>Остаток</span><span class="value" id="left">—</span></div></div>
<script>
(function(){
 const W=window.TelegramWidget;
 const $=id=>document.getElementById(id);
 function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
 function md(s){
  let x=esc(s||'');
  x=x.replace(/```([\s\S]*?)```/g,'<pre>$1</pre>');
  x=x.replace(/`([^`]+)`/g,'<code>$1</code>');
  x=x.replace(/^### (.+)$/gm,'<h3>$1</h3>').replace(/^## (.+)$/gm,'<h2>$1</h2>').replace(/^# (.+)$/gm,'<h2>$1</h2>');
  x=x.replace(/\*\*(.+?)\*\*/gs,'<strong>$1</strong>').replace(/__(.+?)__/gs,'<strong>$1</strong>');
  x=x.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
  return x.split(/\n{2,}/).map(p=>p.startsWith('<pre>')?p:'<p>'+p.replace(/\n/g,'<br>')+'</p>').join('');
 }
 function render(){
  let d={};try{d=JSON.parse(W.chatData||'{}')}catch(e){}
  $('answer').innerHTML=md(d.text||'');
  $('spent').textContent=String(d.spent??0);
  $('left').textContent=d.left==null?'—':String(d.left);
  $('status').textContent=d.status==='done'?'ОТВЕТ ГОТОВ':(d.status==='error'?'НЕ УДАЛОСЬ ПОЛУЧИТЬ ОТВЕТ':'КСЕНИЯ ОТВЕЧАЕТ');
 }
 W.onEvent('chatDataChanged',render);render();
})();
</script></body></html>'''


async def _run_ai(user, msg, prompt: str, context=None) -> None:
    remain = await ai.try_acquire(user.id)
    if remain > 0:
        await msg.reply_text(f"⏳ Подожди {remain:.1f} сек.")
        return

    needed = max(1, ai.count_words(prompt[:ai.MAX_PROMPT_CHARS].strip()))
    initial_balance = await db.get_ai_tokens(user.id)
    is_private = bool(msg.chat and msg.chat.type == "private")
    answer_message = None
    widget_message_id = None
    widget_mode = False
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    bot_token = context.bot.token if context else ""

    async def bot_api(method: str, payload: dict):
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        async with http.post(url, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status != 200 or not data.get("ok"):
                raise RuntimeError(f"Telegram {method}: {data}")
            return data.get("result")

    current_text = ""
    last_update = 0.0
    last_state = ""

    async def publish(text: str, status: str = "streaming", force: bool = False):
        nonlocal last_update, last_state
        output_words = ai.count_words(text)
        spent = needed + output_words
        left = max(0, initial_balance - spent)
        long_answer = len(text.encode("utf-8")) > 660
        visible = _truncate_utf8(text, 660)
        if long_answer and status == "done":
            visible += "\n\nПолный ответ отправлен отдельным сообщением."
        state = json.dumps({"text": visible, "spent": spent, "left": left, "status": status}, ensure_ascii=False, separators=(",", ":"))
        if len(state.encode("utf-8")) > 1000:
            visible = _truncate_utf8(visible, 540)
            state = json.dumps({"text": visible, "spent": spent, "left": left, "status": status}, ensure_ascii=False, separators=(",", ":"))
        if state == last_state and not force:
            return
        now = time.monotonic()
        # editMessageWidgetData считается отправкой сообщения; выдерживаем
        # интервал, чтобы не упираться в лимит Telegram на частые обновления.
        if last_update and now - last_update < 1.1:
            if not force:
                return
            await asyncio.sleep(1.1 - (now - last_update))
        try:
            if widget_mode and widget_message_id:
                await bot_api("editMessageWidgetData", {
                    "chat_id": msg.chat_id,
                    "message_id": widget_message_id,
                    "chat_data": state,
                })
            elif answer_message:
                body = _markdown_to_html(_truncate_utf8(text or "Ксения отвечает…", 2500))
                footer = f"\n\n<blockquote>Потрачено: <b>{spent}</b> · Остаток: <b>{left}</b></blockquote>"
                await answer_message.edit_text((body + footer)[:3900], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            last_state = state
            last_update = now
        except Exception as exc:
            logger.debug("AI progress update failed: %s", exc)

    try:
        if is_private and bot_token:
            try:
                initial_state = json.dumps({"text": "Ксения готовит ответ…", "spent": needed, "left": max(0, initial_balance - needed), "status": "streaming"}, ensure_ascii=False, separators=(",", ":"))
                sent = await bot_api("sendWidget", {
                    "chat_id": msg.chat_id,
                    "html": _WIDGET_HTML,
                    "chat_data": initial_state,
                    "caption": "Ксения отвечает",
                    "allow_queries": False,
                })
                widget_message_id = sent["message_id"]
                widget_mode = True
                last_state = initial_state
            except Exception:
                logger.exception("Не удалось отправить Telegram Widget; включаю обычный вывод")

        if not widget_mode:
            answer_message = await msg.reply_text("Ксения отвечает…", disable_web_page_preview=True)

        async def on_chunk(chunk: str):
            nonlocal current_text
            current_text += chunk
            await publish(current_text)

        answer = await ai.ask_ai_stream(prompt, user_id=user.id, on_chunk=on_chunk)
        if not answer:
            error_text = "ИИ не ответил. Если токены были списаны, они возвращены. Попробуй позже."
            if widget_mode:
                await publish(error_text, "error", force=True)
            elif answer_message:
                await answer_message.edit_text(error_text)
            return

        if answer.startswith(ai.INSUFFICIENT_TOKENS_PREFIX):
            if widget_mode:
                await publish(answer, "error", force=True)
            elif answer_message:
                await answer_message.edit_text(answer)
            return

        output_cost = ai.count_words(answer)
        total_spent = needed + output_cost
        left = await db.get_ai_tokens(user.id)
        # Синхронизируем финальный остаток с реальным балансом БД.
        initial_balance = left + total_spent
        await publish(answer, "done", force=True)
        if widget_mode and widget_message_id:
            try:
                await bot_api("editMessageCaption", {
                    "chat_id": msg.chat_id,
                    "message_id": widget_message_id,
                    "caption": "Ответ Ксении",
                })
            except Exception as exc:
                logger.debug("Не удалось обновить подпись виджета: %s", exc)

        if widget_mode and len(answer.encode("utf-8")) > 660:
            for chunk in _split_telegram_text(answer, limit=2500):
                await msg.reply_text(_markdown_to_html(chunk), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        elif not widget_mode and answer_message:
            body = _markdown_to_html(answer)
            footer = f"\n\n<blockquote>Потрачено: <b>{total_spent}</b> · Остаток: <b>{left}</b></blockquote>"
            if len(body) + len(footer) <= 4096:
                await answer_message.edit_text(body + footer, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            else:
                await answer_message.edit_text("Ответ готов. Полный текст ниже.")
                for chunk in _split_telegram_text(answer, limit=2500):
                    await msg.reply_text(_markdown_to_html(chunk), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    except Exception:
        logger.exception("Ошибка потокового ответа ИИ")
        if answer_message:
            try:
                await answer_message.edit_text("Не удалось вывести ответ. Попробуй ещё раз.")
            except Exception:
                pass
    finally:
        await http.close()


async def cmd_ai(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    msg = update.effective_message

    if not user or not msg:
        return

    text = (msg.text or "").strip()
    match = _AI_WORD.match(text)

    if not match:
        return

    prompt = text[match.end():].strip()

    if not prompt:
        await msg.reply_text(
            "Формат: <code>Ксю Привет</code> или "
            "<code>Ксения Как дела?</code>.\n"
            "1 слово в запросе = 1 токен. "
            "Токены можно купить в «Магазине».",
            parse_mode=ParseMode.HTML,
        )
        return

    await _run_ai(user, msg, prompt, context)


async def cmd_admin_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    u = update.effective_user
    msg = update.effective_message

    if not msg:
        return

    if not ADMIN_ID or not u or u.id != ADMIN_ID:
        await msg.reply_text("⛔ Только для админа.")
        return

    await msg.reply_text(
        "📊 Админ-панель:\n"
        "• +Шериф / -Шериф @user\n"
        "• +Комиссар / -Комиссар @user\n"
        "• Список шерифы / Список комиссары\n"
        "• Поймать @user (шериф)\n"
        "• +Мут 5ч Причина / -Мут @user\n"
        "• +Бан @user / -Бан @user\n"
        "• +Жалоба / +жб @user\n"
        "• +Пред / -Пред @user\n"
        "• +Награда Название / 5 @user\n"
        "• -Награда Название @user\n"
        "• Дать леща / Поцеловать / Обнять / Побить / Обоссать / Погладить\n"
        "• Засос / Расстрел / По шапке / Выстрел / Расцеловать / Сделать 18+\n"
        "• Пожать руку / Станцевать / Обнять всех / Погладить всех / Лещей всем / Тыкнуть / Защекотать\n"
        "• Опрос / описание /1/ Вариант1 / Вариант2 (5 мин)\n"
        "• Опрос ССС / описание /1/ Вариант1 / Вариант2 (5 мин)\n"
        "• Топ все / Топ день / Топ богачи / Мое место\n"
        "• Шип @u1 @u2 (с ИИ)\n"
        "• Сделать предложение / +Брак / -Брак / Мой брак / Топ браки\n"
        "• /MarriedClean @user\n"
        "• Профиль\n"
        "• хо @user 50 — крестики (комиссия 5%)\n"
        "• +Кредит 50 / -Кредит 50\n"
        "• Сберкнижка / +Сберкнижка 500 / -Сберкнижка 200\n"
        "• +Монеты / -Монеты @user\n"
        "• +Утил / -Утил @user / казна\n"
        "• Перевод 50 @user\n"
        "• Казна / +ВКассу 500 / ОбрезатьКасну 15000\n"
        "• Зарплата\n"
        "• +ССС / -ССС / Список ССС\n"
        "• +Суд ССС / +Суд\n"
        "• дроч / Аура / Топ аура\n"
        "• Казино монетка/кубик/слоты\n"
        "• Ксюша / Ксения / Ксю <запрос>\n"
        "• Правила / Магазин\n"
        "• -смс\n"
        "• /Defloult220099\n"
        "• /ALLMONEY 100\n"
        "• /restart /help"
    )


async def _cleanup_polls_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        n = polls.cleanup_expired_polls()

        if n:
            logger.info("polls cleanup: removed %d", n)

    except Exception as e:
        logger.warning("polls cleanup: %s", e)


async def post_init(app: Application) -> None:
    await db.init_db()
    await helper.init_db()
    await sss.restore_sss_members()
    await resolver.start_client()
    await commands_dop_handlers.sync_to_db()

    if app.job_queue:
        app.job_queue.run_daily(
            top.cleanup_job,
            time=dtime(hour=0, minute=5, tzinfo=timezone.utc),
        )

        app.job_queue.run_repeating(
            top.flush_job,
            interval=top.FLUSH_INTERVAL_SEC,
            first=top.FLUSH_INTERVAL_SEC,
        )

        app.job_queue.run_repeating(
            _cleanup_polls_job,
            interval=60,
            first=60,
        )

        economy.register_jobs(app)

    else:
        logger.warning("JobQueue не инициализирован")


async def post_shutdown(app: Application) -> None:
    try:
        await top._flush()
    except Exception as e:
        logger.warning("flush on shutdown: %s", e)

    await resolver.stop_client()
    await ai.close_session()
    await db.close_db()

    logger.info("Bot stopped")


def build_app() -> Application:
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_error_handler(_error_handler)

    # Личный кабинет предложки: обычные пользователи не попадают
    # в обработчики групповых команд, экономики и активности.
    # Администратор сохраняет существующий функционал в личке.
    app.add_handler(
        TypeHandler(
            Update,
            lambda update, context: helper.handle_private_update(
                update, context, ADMIN_ID
            ),
        ),
        group=-2,
    )

    # Глобальный фильтр заблокированных пользователей.
    app.add_handler(
        TypeHandler(
            Update,
            blacklist.guard_blacklisted_update,
        ),
        group=-3,
    )

    app.add_handler(CommandHandler("banbot", blacklist.cmd_banbot))
    app.add_handler(CommandHandler("unbanbot", blacklist.cmd_unbanbot))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(CommandHandler("check", pr.recheck_subscription))
    app.add_handler(CommandHandler("help", help_menu.cmd_help))
    app.add_handler(
        CallbackQueryHandler(
            help_menu.help_callback,
            pattern=r"^help:",
        )
    )
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("casino", casino.cmd_casino))
    app.add_handler(
        CommandHandler("auraplush", aura_admin.cmd_aura_plush)
    )
    app.add_handler(
        CommandHandler("Defloult220099", economy.cmd_force_default)
    )
    app.add_handler(
        CommandHandler("ALLMONEY", economy.cmd_all_money)
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"^\+Шериф(\s|$)"),
        roles.grant_sheriff,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[-–−]Шериф(\s|$)"),
        roles.revoke_sheriff,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^\+Комиссар(\s|$)"),
        roles.grant_commissar,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[-–−]Комиссар(\s|$)"),
        roles.revoke_commissar,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Список\s+шерифы(\s|$)"),
        roles.list_sheriffs,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Список\s+комиссары(\s|$)"),
        roles.list_commissars,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Поймать(\s|$)"),
        roles.sheriff_catch,
    ))
    app.add_handler(
        CallbackQueryHandler(
            roles.catch_callback,
            pattern=r"^catch:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^-смс\s*$"),
        cmd_delete_message,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Мут(\s|$)"),
        roles.mute_user,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Мут(\s|$)"),
        roles.unmute_user,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Бан(\s|$)"),
        roles.ban_user,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Бан(\s|$)"),
        roles.unban_user,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Баланс\s*$"),
        economy.cmd_balance,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Сберкнижка\s*$"),
        economy.cmd_savings_book,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Сберкнижка(\s|$)"),
        economy.cmd_savings_deposit,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Сберкнижка(\s|$)"),
        economy.cmd_savings_withdraw,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Кредит(\s|$)"),
        economy.cmd_credit_take,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Кредит(\s|$)"),
        economy.cmd_credit_repay,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Монеты(\s|$)"),
        economy.cmd_admin_coins,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Монеты(\s|$)"),
        economy.cmd_admin_coins_minus,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Перевод(\s|$)"),
        economy.cmd_transfer,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Казна(\s|$)"),
        economy.cmd_treasury,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+ВКассу(\s|$)"),
        economy.cmd_treasury_add,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^ОбрезатьКасну(\s|$)"),
        economy.cmd_treasury_trim,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Зарплата(\s|$)"),
        economy.cmd_salary,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+богачи(\s|$)"),
        top.cmd_top_rich,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Утил\s+\d+\s+казна(\s|$)"),
        economy.cmd_burn_treasury,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Утил(\s|$)"),
        economy.cmd_burn_user,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Утил\s+\d+\s+казна(\s|$)"),
        economy.cmd_mint_treasury,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Утил(\s|$)"),
        economy.cmd_mint_user,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^казино(\s|$)"),
        casino.cmd_casino,
    ))
    app.add_handler(
        CallbackQueryHandler(
            casino.casino_help_callback,
            pattern=r"^cas:help:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+все(\s|$)"),
        top.cmd_top_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+день(\s|$)"),
        top.cmd_top_day,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Мое\s+место(\s|$)"),
        top.cmd_my_place,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Шип(\s|$)"),
        ship.cmd_ship,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Сделать\s+предложение(\s|$)"),
        wedding.cmd_propose,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Брак(\s|$)"),
        wedding.cmd_force_divorce_or_self,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Брак(\s|$)"),
        wedding.cmd_force_marriage,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Мой\s+брак(\s|$)"),
        wedding.cmd_marriage_info,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+браки(\s|$)"),
        wedding.cmd_marriage_top,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^/marriedclean(?:@\w+)?(?:\s|$)"),
        wedding.cmd_married_clean,
    ))
    app.add_handler(
        CallbackQueryHandler(
            wedding.wedding_callback,
            pattern=r"^wed:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Репутация(?:\s+.*)?$"),
        reputation.cmd_reputation,
    ))
    app.add_handler(MessageHandler(
        filters.REPLY & filters.Regex(r"^[+-]$"),
        reputation.cmd_vote,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Профиль(\s|$)"),
        profile.cmd_profile,
    ))
    app.add_handler(
        CallbackQueryHandler(
            profile.profile_callback,
            pattern=r"^prof:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^хо(\s|$)"),
        tictactoe.cmd_challenge,
    ))
    app.add_handler(
        CallbackQueryHandler(
            tictactoe.ttt_callback,
            pattern=r"^ttt:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+ССС(\s|$)"),
        sss.cmd_sss_add,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]ССС(\s|$)"),
        sss.cmd_sss_remove,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Список\s+ССС(\s|$)"),
        sss.cmd_sss_list,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Суд\s+ССС(\s|$)"),
        sss.cmd_sss_trial,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Суд(\s|$)"),
        sss.cmd_public_trial,
    ))
    app.add_handler(
        CallbackQueryHandler(
            sss.sss_trial_callback,
            pattern=r"^ssstrial:",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            sss.public_trial_callback,
            pattern=r"^pubtrial:",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            sss.public_admin_callback,
            pattern=r"^pubadm:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(rules.RULES_RE),
        rules.cmd_rules,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Опрос\s+ССС(\s|/)"),
        polls.cmd_poll_sss,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Опрос(?!\s+ССС)(\s|/)"),
        polls.cmd_poll_public,
    ))
    app.add_handler(
        CallbackQueryHandler(
            polls.poll_callback,
            pattern=r"^poll:",
        )
    )

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+(Жалоба|ЖБ|Жал)(\s|$)"),
        complaints.cmd_complaint,
    ))
    app.add_handler(
        CallbackQueryHandler(
            complaints.complaint_callback,
            pattern=r"^compl:",
        )
    )
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+(Предупреждение|Пред|Варн)(\s|$)"),
        warns.cmd_warn,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−](Предупреждение|Пред|Варн)(\s|$)"),
        warns.cmd_unwarn,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^дроч(\s|$)"),
        aura.cmd_droch,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Топ\s+аура(\s|$)"),
        aura.cmd_aura_top,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Аура(\s|$)"),
        aura.cmd_aura,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^\+Награда(\s|$)"),
        awards.cmd_award,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^[-–−]Награда(\s|$)"),
        awards.cmd_unaward,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Дать\s+леща(\s|$)"),
        actions.cmd_slap,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Поцеловать(\s|$)"),
        actions.cmd_kiss,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Обнять(\s|$)"),
        actions.cmd_hug,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Побить(\s|$)"),
        actions.cmd_hit,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Обоссать(\s|$)"),
        actions.cmd_piss,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Погладить(\s|$)"),
        actions.cmd_pat,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Засос(\s|$)"),
        actions.cmd_kiss_deep,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Расстрел(\s|$)"),
        actions.cmd_shoot,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Дать\s+по\s+шапке(\s|$)"),
        actions.cmd_bonk,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Выстрел(\s|$)"),
        actions.cmd_gunshot,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Расцеловать(\s|$)"),
        actions.cmd_kiss_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Сделать\s+18\+?(\s|$)"),
        actions.cmd_make18,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Станцевать(\s|$)"),
        actions.cmd_dance,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Обнять\s+всех(\s|$)"),
        actions.cmd_hug_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Погладить\s+всех(\s|$)"),
        actions.cmd_pat_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Лещей\s+всем(\s|$)"),
        actions.cmd_slap_all,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Тыкнуть(\s|$)"),
        actions.cmd_poke,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Защекотать(\s|$)"),
        actions.cmd_tickle,
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^(Пожать\s+руку|Рукопожатие)(\s|$)"),
        actions.cmd_handshake,
    ))

    # ИИ Ксения — только обращения к Ксюше.
    app.add_handler(MessageHandler(
        filters.Regex(_AI_WORD),
        cmd_ai,
    ))

    app.add_handler(MessageHandler(
        filters.Regex(r"(?i)^Магазин(\s|$)"),
        shop.cmd_shop,
    ))
    app.add_handler(
        CallbackQueryHandler(
            shop.shop_callback,
            pattern=r"^shop:",
        )
    )

    commands_dop_handlers.register(app)

    if CHAT_ID:
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=CHAT_ID)
                & ~filters.COMMAND
                & ~filters.StatusUpdate.ALL,
                top.track_message,
            ),
            group=1,
        )

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            pr.on_new_member,
        )
    )

    if CHAT_ID:
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=CHAT_ID)
                & ~filters.COMMAND
                & ~filters.StatusUpdate.ALL,
                pr.check_and_delete,
            )
        )

    return app


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN не задан")

    if not WEBHOOK_URL:
        raise SystemExit("WEBHOOK_URL не задан")

    if not WEBHOOK_URL.startswith("https://"):
        raise SystemExit("WEBHOOK_URL должен начинаться с https://")

    app = build_app()

    logger.info("Starting webhook server on port %s", PORT)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_PATH}",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
