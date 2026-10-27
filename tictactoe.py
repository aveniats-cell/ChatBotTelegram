"""Крестики-нолики 5×5 на монеты.

P0: эскроу ставок. При accept списываем bet у обоих; при finish:
- ничья: каждому +bet (возврат своей ставки)
- победа: победителю +2*bet (своя + чужая), проигравший уже потерял свою.
Никакого надувания экономики.
"""
import os
import re
import uuid
import logging
from html import escape
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import utils

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")
BET_RE = re.compile(r"\b(\d+)\b")

BOARD_SIZE = 5
WIN_LEN = 5

GAMES: dict[str, dict] = {}


def _empty_board() -> list[list[str]]:
    return [["" for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def _board_keyboard(game_id: str, board: list[list[str]],
                    disabled: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for r in range(BOARD_SIZE):
        row = []
        for c in range(BOARD_SIZE):
            v = board[r][c]
            label = "·" if not v else v
            cb = "ttt:noop" if disabled or v else f"ttt:{game_id}:{r}:{c}"
            row.append(InlineKeyboardButton(label, callback_data=cb))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _render_board(board: list[list[str]]) -> str:
    return "\n".join(" ".join(cell or "·" for cell in row) for row in board)


def _check_win(board: list[list[str]], symbol: str) -> bool:
    dirs = ((0, 1), (1, 0), (1, 1), (1, -1))
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            if board[r][c] != symbol:
                continue
            for dr, dc in dirs:
                ok = True
                for k in range(WIN_LEN):
                    rr, cc = r + dr * k, c + dc * k
                    if not (0 <= rr < BOARD_SIZE and 0 <= cc < BOARD_SIZE):
                        ok = False
                        break
                    if board[rr][cc] != symbol:
                        ok = False
                        break
                if ok:
                    return True
    return False


def _is_full(board: list[list[str]]) -> bool:
    return all(all(cell for cell in row) for row in board)


async def cmd_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    chat = update.effective_chat
    if not user or not msg or not chat:
        return

    text = msg.text or ""

    matches = list(BET_RE.finditer(text))
    if not matches:
        await msg.reply_text(
            "Формат: <code>хо @user 50</code> или реплай + <code>хо 50</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    bet_match = matches[-1]
    bet = int(bet_match.group(1))
    if bet <= 0:
        await msg.reply_text("⛔ Ставка должна быть больше 0.")
        return

    clean = text[:bet_match.start()] + text[bet_match.end():]

    opponent = await utils.resolve_target(update, context, clean)
    if not opponent:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    opp_id, opp_name = opponent
    if opp_id == user.id:
        await msg.reply_text("Себе вызов бросить нельзя 🙂")
        return

    me_row = await db.get_user(user.id)
    if not me_row:
        await db.upsert_user(user.id, user.username, user.first_name)
        me_row = await db.get_user(user.id)
    opp_row = await db.get_user(opp_id)
    if not opp_row:
        await db.upsert_user(opp_id, None, opp_name)
        opp_row = await db.get_user(opp_id)

    my_balance = (me_row or {}).get("balance") or 0
    opp_balance = (opp_row or {}).get("balance") or 0
    if my_balance < bet:
        await msg.reply_text(
            f"⛔ У вас недостаточно монет: <b>{my_balance}</b> < <b>{bet}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return
    if opp_balance < bet:
        await msg.reply_text(
            f"⛔ У соперника недостаточно монет: <b>{opp_balance}</b> < <b>{bet}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    game_id = uuid.uuid4().hex[:10]
    GAMES[game_id] = {
        "chat_id": chat.id,
        "p1_id": user.id,
        "p1_name": user.first_name or user.username or str(user.id),
        "p2_id": opp_id,
        "p2_name": opp_name,
        "bet": bet,
        "board": _empty_board(),
        "turn": user.id,
        "symbols": {user.id: "❌", opp_id: "⭕"},
        "state": "pending",
        "escrow_taken": False,
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Согласиться", callback_data=f"ttt:accept:{game_id}"),
        InlineKeyboardButton("❌ Отказаться", callback_data=f"ttt:decline:{game_id}"),
    ]])
    await msg.reply_text(
        f"🎮 <b>Вызов!</b>\n"
        f"{escape(GAMES[game_id]['p1_name'])} → {escape(opp_name)}\n"
        f"Ставка: <b>{bet}</b> монет.\n\n"
        f"<i>{escape(opp_name)}, вы принимаете вызов?</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _finish_game(context: ContextTypes.DEFAULT_TYPE, game_id: str,
                       winner_id: Optional[int]) -> None:
    g = GAMES.get(game_id)
    if not g or g["state"] == "finished":
        return
    g["state"] = "finished"

    bet = g["bet"]
    p1, p2 = g["p1_id"], g["p2_id"]

    if not g.get("escrow_taken"):
        logger.warning("finish_game: escrow не списан для %s", game_id)

    if winner_id is None:
        await db.add_balance(p1, bet)
        await db.add_balance(p2, bet)
        text = f"🤝 Ничья! Ставки возвращены по <b>{bet}</b>."
    elif winner_id == p1:
        await db.add_balance(p1, bet * 2)
        text = (
            f"🏆 Победил <b>{escape(g['p1_name'])}</b>!\n"
            f"Забрано: <b>{bet * 2}</b> (своя + чужая ставка)."
        )
    else:
        await db.add_balance(p2, bet * 2)
        text = (
            f"🏆 Победил <b>{escape(g['p2_name'])}</b>!\n"
            f"Забрано: <b>{bet * 2}</b> (своя + чужая ставка)."
        )

    try:
        if winner_id == p1:
            await db.inc_user_counter(p1, "tictactoe_wins", 1)
            await db.inc_user_counter(p2, "tictactoe_losses", 1)
        elif winner_id == p2:
            await db.inc_user_counter(p2, "tictactoe_wins", 1)
            await db.inc_user_counter(p1, "tictactoe_losses", 1)
        else:
            await db.inc_user_counter(p1, "tictactoe_draws", 1)
            await db.inc_user_counter(p2, "tictactoe_draws", 1)
    except Exception as e:
        logger.warning("finish_game counters: %s", e)

    try:
        await context.bot.send_message(
            chat_id=g["chat_id"],
            text=f"🎮 <b>Игра окончена</b>\n{text}\n\n{_render_board(g['board'])}",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("finish_game send: %s", e)

    GAMES.pop(game_id, None)


async def ttt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = update.effective_user
    if not q or not user:
        return
    data = q.data or ""
    if not data.startswith("ttt:"):
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "noop":
        await q.answer()
        return

    if action in ("accept", "decline"):
        if len(parts) != 3:
            await q.answer()
            return
        game_id = parts[2]
        g = GAMES.get(game_id)
        if not g:
            await q.answer("Игра не найдена.", show_alert=True)
            return
        if g["state"] != "pending":
            await q.answer("Вызов уже обработан.", show_alert=True)
            return
        if user.id != g["p2_id"]:
            await q.answer("Этот вызов не вам.", show_alert=True)
            return

        if action == "decline":
            g["state"] = "declined"
            GAMES.pop(game_id, None)
            await q.answer("Отклонено")
            try:
                await q.edit_message_text(
                    (q.message.text or "") + "\n\n❌ <b>Отклонено.</b>",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            return

        # ACCEPT: эскроу — списываем ставку у обоих
        bet = g["bet"]
        p1, p2 = g["p1_id"], g["p2_id"]

        r1 = await db.get_user(p1)
        r2 = await db.get_user(p2)
        b1 = (r1 or {}).get("balance") or 0
        b2 = (r2 or {}).get("balance") or 0
        if b1 < bet or b2 < bet:
            g["state"] = "declined"
            GAMES.pop(game_id, None)
            await q.answer("У кого-то не хватает монет.", show_alert=True)
            try:
                await q.edit_message_text(
                    (q.message.text or "") + "\n\n⚠️ <b>Отменено: недостаточно монет.</b>",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            return

        await db.add_balance(p1, -bet)
        await db.add_balance(p2, -bet)
        g["escrow_taken"] = True
        g["state"] = "active"

        await q.answer("Поехали! Ставки списаны.")
        kb = _board_keyboard(game_id, g["board"])
        text = (
            f"🎮 <b>Крестики-нолики 5×5</b>\n"
            f"{escape(g['p1_name'])} {g['symbols'][g['p1_id']]} vs "
            f"{escape(g['p2_name'])} {g['symbols'][g['p2_id']]}\n"
            f"Ставка: <b>{bet}</b> | Собрать 5 в ряд.\n"
            f"Ход: {escape(g['p1_name'])}"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if len(parts) == 4:
        game_id, r_s, c_s = parts[1], parts[2], parts[3]
        try:
            r, c = int(r_s), int(c_s)
        except ValueError:
            await q.answer()
            return
        g = GAMES.get(game_id)
        if not g:
            await q.answer("Игра не найдена.", show_alert=True)
            return
        if g["state"] != "active":
            await q.answer()
            return
        if user.id not in (g["p1_id"], g["p2_id"]):
            await q.answer("Вы не участник.", show_alert=True)
            return
        if user.id != g["turn"]:
            await q.answer("Сейчас не ваш ход.", show_alert=True)
            return
        if g["board"][r][c]:
            await q.answer("Клетка занята.", show_alert=True)
            return

        symbol = g["symbols"][user.id]
        g["board"][r][c] = symbol

        if _check_win(g["board"], symbol):
            await q.answer("Победа!")
            try:
                await q.edit_message_reply_markup(
                    reply_markup=_board_keyboard(game_id, g["board"], disabled=True)
                )
            except TelegramError:
                pass
            await _finish_game(context, game_id, winner_id=user.id)
            return

        if _is_full(g["board"]):
            await q.answer("Ничья")
            try:
                await q.edit_message_reply_markup(
                    reply_markup=_board_keyboard(game_id, g["board"], disabled=True)
                )
            except TelegramError:
                pass
            await _finish_game(context, game_id, winner_id=None)
            return

        g["turn"] = g["p2_id"] if user.id == g["p1_id"] else g["p1_id"]
        next_name = g["p1_name"] if g["turn"] == g["p1_id"] else g["p2_name"]

        await q.answer()
        try:
            await q.edit_message_text(
                f"🎮 <b>Крестики-нолики 5×5</b>\n"
                f"{escape(g['p1_name'])} {g['symbols'][g['p1_id']]} vs "
                f"{escape(g['p2_name'])} {g['symbols'][g['p2_id']]}\n"
                f"Ставка: <b>{g['bet']}</b>\n"
                f"Ход: {escape(next_name)}",
                parse_mode=ParseMode.HTML,
                reply_markup=_board_keyboard(game_id, g["board"]),
            )
        except TelegramError as e:
            logger.warning("ttt edit: %s", e)
        return

    await q.answer()
