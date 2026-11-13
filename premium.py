"""Telegram Stars Premium: subscriptions, recurring rewards and administration."""
import logging, os
from datetime import datetime, timedelta, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
import db

log = logging.getLogger(__name__)
ADMIN_ID = int(os.getenv('ADMIN_ID', '0') or 0)
PLANS = {
 'pro': {'title':'Premium Pro 💎','days':29,'price':12,'coins':400,'daily_coins':25,'tokens':1000,'daily_tokens':10,'renew':10,'badge':'💎','packs':'all'},
 'base': {'title':'Premium Base 🤑','days':25,'price':6,'coins':100,'daily_coins':10,'tokens':100,'daily_tokens':0,'renew':5,'badge':'🤑','packs':'one'},
}

async def init_db():
 await db._exec('''CREATE TABLE IF NOT EXISTS premium_subscriptions (user_id INTEGER PRIMARY KEY, plan TEXT NOT NULL, expires_at TEXT NOT NULL, last_reward_day TEXT, base_pocket_id INTEGER, free_credit_used INTEGER NOT NULL DEFAULT 0)''')
 await db._exec('''CREATE TABLE IF NOT EXISTS premium_payments (charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL)''')
 await db._exec('''CREATE TABLE IF NOT EXISTS coin_shop_payments (charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, coins INTEGER NOT NULL, created_at TEXT NOT NULL)''')

async def get(user_id):
 r=await db._exec('SELECT plan, expires_at, base_pocket_id FROM premium_subscriptions WHERE user_id=?',[user_id])
 if not r.rows:return None
 p,e,b=r.rows[0]
 try: active=datetime.fromisoformat(str(e)) > datetime.now(timezone.utc)
 except Exception: active=False
 return {'plan':p,'expires_at':str(e),'base_pocket_id':b,'active':active} if active else None

async def has_protection(user_id): return bool(await get(user_id))

async def open_menu(q):
 await q.edit_message_text('🛍 <b>МАГАЗИН</b>\n\nВыберите, что хотите приобрести:',parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🪙 Купить монеты за Stars',callback_data='shop:coins')],[InlineKeyboardButton('⭐ Premium-подписка',callback_data='shop:premium')],[InlineKeyboardButton('← Назад',callback_data='shop:main')]]))
 await q.answer()

async def show_catalog(q):
 await q.edit_message_text('⭐ <b>PREMIUM</b>\n\nВыберите подписку:',parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('💎 Premium Pro',callback_data='shop:premium_plan:pro')],[InlineKeyboardButton('🤑 Premium Base',callback_data='shop:premium_plan:base')],[InlineKeyboardButton('← В магазин',callback_data='shop:premium_home')]]))
 await q.answer()

async def show_plan(q, plan):
 p=PLANS[plan]
 if plan=='pro':
  perks='''1. 1 бесплатное полное списание всех кредитов\n2. Защита от дефолтов\n3. +25 монет ежедневно\n4. +400 монет сразу\n5. +1000 токенов ИИ сразу\n6. +10 токенов ИИ ежедневно\n7. Ускоренное восстановление (30 минут)\n8. Статус Premium Pro 💎 в профиле\n9. 💎 рядом с ником в топах\n10. Все платные паки команд без ограничений'''
 else:
  perks='''1. Защита от дефолтов\n2. +100 монет сразу\n3. +10 монет ежедневно\n4. +100 токенов ИИ сразу\n5. 🤑 рядом с ником в топах\n6. Статус Premium Base 🤑 в профиле\n7. Один платный пак команд на выбор'''
 text=f"<b>{p['title']}</b>\n\nЦена: <b>{p['price']}⭐</b>\nСрок действия: <b>{p['days']} дней</b>{' + 2🎁' if plan=='pro' else ''}\n\n<b>Разблокируется:</b>\n{perks}"
 await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Оплатить {p['price']} ⭐",callback_data=f'shop:premium_pay:{plan}')],[InlineKeyboardButton('← Каталог',callback_data='shop:premium')]]))
 await q.answer()

async def choose_base_pack(q):
 pockets=await db.shop_pockets_list(q.message.chat.id)
 rows=[]
 for item in pockets or []:
  if item.get('active'):
   rows.append([InlineKeyboardButton(f"{item['name']} · {item['price']}🪙",callback_data=f"shop:premium_pack:{int(item['id'])}")])
 rows.append([InlineKeyboardButton('← Назад',callback_data='shop:premium_plan:base')])
 await q.edit_message_text('🤑 <b>Выберите один платный пакет</b>, который будет доступен на время Premium Base:',parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))
 await q.answer()

async def send_invoice(q, context, user, plan):
 p=PLANS[plan]
 payload=f"premium:{plan}:{user.id}:{int(datetime.now(timezone.utc).timestamp())}:{int(context.user_data.get('premium_base_pack', 0) or 0)}"
 await context.bot.send_invoice(chat_id=q.message.chat_id,title=p['title'],description=f"Подписка на {p['days']} дней",payload=payload,provider_token='',currency='XTR',prices=[LabeledPrice(p['title'],p['price'])])
 await q.answer('Счёт отправлен. Оплати его сообщением выше.')

async def precheckout(update:Update, context:ContextTypes.DEFAULT_TYPE):
 q=update.pre_checkout_query
 if not q:return
 parts=(q.invoice_payload or '').split(':')
 try:
  if len(parts)==4 and parts[0]=='coins':
   coins=int(parts[2])
   ok=(int(parts[1])==q.from_user.id and 20<=coins<=200000 and coins%20==0 and q.currency=='XTR' and q.total_amount==coins//20)
  else:
   ok=(len(parts)==5 and parts[0]=='premium' and parts[1] in PLANS and int(parts[2])==q.from_user.id and q.currency=='XTR' and q.total_amount==PLANS[parts[1]]['price'])
 except (TypeError, ValueError):
  ok=False
 await q.answer(ok=ok,error_message=None if ok else 'Счёт недействителен. Открой магазин и создай новый.')

async def activate(user, plan, context, charge_id, chat_id=None, base_pocket_id=None):
 p=PLANS[plan]
 if charge_id:
  ins=await db._exec('INSERT OR IGNORE INTO premium_payments(charge_id,user_id,plan,created_at) VALUES(?,?,?,?)',[charge_id,user.id,plan,datetime.now(timezone.utc).isoformat()])
  if getattr(ins,'error',False) or not ins.rows_affected:return False
 old=await get(user.id)
 now=datetime.now(timezone.utc)
 expiry=now+timedelta(days=p['days'])
 if old and old['plan']==plan:
  try: expiry=max(now,datetime.fromisoformat(old['expires_at']))+timedelta(days=p['days'])
  except Exception: pass
 await db.upsert_user(user.id,user.username,user.first_name or '')
 await db._exec('''INSERT INTO premium_subscriptions(user_id,plan,expires_at,last_reward_day,base_pocket_id,free_credit_used) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan,expires_at=excluded.expires_at,last_reward_day=excluded.last_reward_day,base_pocket_id=excluded.base_pocket_id''',[user.id,plan,expiry.isoformat(),now.date().isoformat(),base_pocket_id,1 if plan=='pro' else 0])
 await db.add_balance(user.id,p['coins'])
 await db.refund_ai_tokens(user.id,p['tokens'])
 if plan=='pro':
  await db._exec('UPDATE users SET credit=0 WHERE user_id=?',[user.id])
  # ускоренное восстановление и символ/статус считываются из активной подписки
 receipt=(f"🧾 <b>Чек Premium</b>\nКому: @{user.username or user.id}\nОт кого: @YouWarm\nЦена: {p['price']}⭐\nУслуга: покупка премиума — {p['title']}\nКогда выдано: {now.strftime('%d.%m.%Y %H:%M UTC')}\nКогда истечет: {expiry.strftime('%d.%m.%Y %H:%M UTC')}")
 try: await context.bot.send_message(user.id,receipt,parse_mode=ParseMode.HTML)
 except Exception: log.exception('Не удалось отправить чек покупателю')
 admin=ADMIN_ID
 if admin:
  try: await context.bot.send_message(admin,receipt,parse_mode=ParseMode.HTML)
  except Exception: log.exception('Не удалось отправить чек админу')
 if chat_id:
  try: await context.bot.send_message(chat_id,f"🎉 {user.mention_html()} активировал(а) {p['title']}!",parse_mode=ParseMode.HTML)
  except Exception: pass
 return True

async def successful_payment(update:Update,context:ContextTypes.DEFAULT_TYPE):
 msg=update.effective_message; user=update.effective_user
 sp=msg.successful_payment if msg else None
 if not sp or not user:return
 parts=(sp.invoice_payload or '').split(':')
 if parts and parts[0]=='coins':
  try:
   valid=(len(parts)==4 and int(parts[1])==user.id and int(parts[2])>0 and int(parts[2])%20==0 and sp.currency=='XTR' and sp.total_amount==int(parts[2])//20)
  except (TypeError,ValueError): valid=False
  if not valid:
   log.error('Invalid coin purchase payment payload for user %s',user.id); return
  coins=int(parts[2]); charge=sp.telegram_payment_charge_id
  ins=await db._exec('INSERT OR IGNORE INTO coin_shop_payments(charge_id,user_id,coins,created_at) VALUES(?,?,?,?)',[charge,user.id,coins,datetime.now(timezone.utc).isoformat()])
  if getattr(ins,'error',False):
   log.error('Could not record coin payment %s',charge); return
  if not ins.rows_affected:return
  await db.upsert_user(user.id,user.username,user.first_name or '')
  if not await db.add_balance(user.id,coins):
   log.error('Payment recorded but coin credit failed: user=%s coins=%s charge=%s',user.id,coins,charge); return
  await msg.reply_text(f'✅ Оплата прошла! На баланс начислено {coins} 🪙.')
  return
 if len(parts)!=5 or parts[0]!='premium' or parts[1] not in PLANS:return
 try: valid=int(parts[2])==user.id and sp.currency=='XTR' and sp.total_amount==PLANS[parts[1]]['price']
 except (TypeError,ValueError): valid=False
 if not valid:return
 ok=await activate(user,parts[1],context,sp.telegram_payment_charge_id,chat_id=msg.chat_id,base_pocket_id=(int(parts[4]) or None))
 if ok: await msg.reply_text('✅ Premium активирован! Чек отправлен в личные сообщения.')

async def start_coin_purchase(q, context):
 from telegram import ForceReply
 context.user_data['awaiting_coin_amount']=True
 await q.message.reply_text('🪙 Введите количество монет.\n\nКурс: 1 монета = 0,05 ⭐.\nМинимум — 20 монет (1 ⭐), количество должно делиться на 20.',reply_markup=ForceReply(selective=True))
 await q.answer('Введите количество монет ответом на сообщение бота.')

async def coin_amount_input(update:Update, context:ContextTypes.DEFAULT_TYPE):
 msg=update.effective_message; user=update.effective_user
 if not msg or not user or not context.user_data.get('awaiting_coin_amount') or not msg.reply_to_message:return
 try: coins=int((msg.text or '').strip())
 except ValueError:
  await msg.reply_text('Введите целое число монет, например 100.'); return
 if coins<20 or coins>200000 or coins%20:
  await msg.reply_text('Количество должно быть от 20 до 200 000 и делиться на 20. Например: 100.'); return
 context.user_data.pop('awaiting_coin_amount',None)
 stars=coins//20
 payload=f'coins:{user.id}:{coins}:{int(datetime.now(timezone.utc).timestamp())}'
 await context.bot.send_invoice(chat_id=msg.chat_id,title=f'{coins} монет',description=f'Пополнение баланса на {coins} монет по курсу 0,05 ⭐ за монету',payload=payload,provider_token='',currency='XTR',prices=[LabeledPrice(f'{coins} монет',stars)])
 await msg.reply_text(f'🧾 К оплате: {coins} 🪙 × 0,05 ⭐ = {stars} ⭐\nНажмите кнопку оплаты в счёте выше.')

async def rewards_job(context):
 today=datetime.now(timezone.utc).date().isoformat()
 rs=await db._exec('SELECT user_id,plan,expires_at,last_reward_day FROM premium_subscriptions')
 for uid,plan,expires,last in rs.rows:
  try: active=datetime.fromisoformat(str(expires))>datetime.now(timezone.utc)
  except Exception: active=False
  if not active:
   await db._exec('DELETE FROM premium_subscriptions WHERE user_id=?',[uid])
   try: await context.bot.send_message(int(uid),'⌛ Срок действия Premium истёк. Продлить премиум?\n',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Продлить за {PLANS[plan]['renew']}⭐",callback_data=f'shop:premium_renew:{plan}')]]))
   except Exception: pass
   continue
  if last!=today:
   p=PLANS[plan]; await db.add_balance(int(uid),p['daily_coins'])
   if p['daily_tokens']: await db.refund_ai_tokens(int(uid),p['daily_tokens'])
   await db._exec('UPDATE premium_subscriptions SET last_reward_day=? WHERE user_id=?',[today,uid])

async def admin_command(update:Update,context:ContextTypes.DEFAULT_TYPE):
 msg=update.effective_message; actor=update.effective_user
 if not msg:return
 if not actor or actor.id!=ADMIN_ID: await msg.reply_text('⛔ Только для администратора.'); return
 if len(context.args)!=4 or context.args[0] not in ('+','-') or context.args[1].lower() not in PLANS:
  await msg.reply_text('Формат: /GetPremium +/- Base/Pro количество_дней @username'); return
 op,plan,days,target=context.args; uid=await db.get_user_by_username(target.lstrip('@'))
 if not uid: await msg.reply_text('Пользователь не найден в базе.'); return
 if op=='-': await db._exec('DELETE FROM premium_subscriptions WHERE user_id=? AND plan=?',[uid,plan.lower()]); await msg.reply_text('Premium снят.'); return
 days=int(days)
 if days<1 or days>3650: await msg.reply_text('Укажи от 1 до 3650 дней.'); return
 await db._exec('''INSERT INTO premium_subscriptions(user_id,plan,expires_at,last_reward_day) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan,expires_at=excluded.expires_at,last_reward_day=excluded.last_reward_day''',[uid,plan.lower(),(datetime.now(timezone.utc)+timedelta(days=days)).isoformat(),datetime.now(timezone.utc).date().isoformat()])
 await msg.reply_text(f'✅ {plan} выдан на {days} дней пользователю @{target.lstrip("@")}')
