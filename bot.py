"""
Установите зависимости:
pip install aiogram==3.3.0 aiosqlite yookassa APScheduler

ВАЖНО: 
- Получите ID канала (инструкция ниже)
- Настройте ЮКассу
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove
)
from yookassa import Configuration, Payment
import uuid

# ======================== КОНФИГУРАЦИЯ ========================
BOT_TOKEN = "8379357573:AAGI7G4U9Uon1-CtcweaPBP9PImZjtBFniU"
YOOKASSA_SHOP_ID = "ваш_shop_id"  # Получите на yookassa.ru
YOOKASSA_SECRET_KEY = "ваш_secret_key"  # Получите на yookassa.ru

# КАК ПОЛУЧИТЬ ID КАНАЛА:
# 1. Добавьте бота @userinfobot в ваш канал https://t.me/+iG_xlgpdOaQ5NTUy
# 2. Перешлите любое сообщение из канала боту @userinfobot
# 3. Он пришлет ID канала (например: -1001234567890)
# 4. Замените значение ниже на полученный ID
CHANNEL_ID = -1001234567890  # ЗАМЕНИТЕ НА ID ВАШЕГО КАНАЛА!

ADMIN_IDS = [7338817463, 1478525032]
PRICE_1_MONTH = 1111
WELCOME_PHOTO = "f.png"

# Настройка ЮКassa
Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

# ======================== ИНИЦИАЛИЗАЦИЯ ========================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# ======================== FSM СОСТОЯНИЯ ========================
class BroadcastStates(StatesGroup):
    waiting_message = State()
    selecting_date = State()
    selecting_time = State()
    confirming = State()

class PhoneState(StatesGroup):
    waiting_phone = State()

class WelcomePhotoState(StatesGroup):
    waiting_photo = State()

# ======================== БАЗА ДАННЫХ ========================
async def init_db():
    async with aiosqlite.connect('bot.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                phone TEXT,
                subscription_until INTEGER,
                created_at INTEGER
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_text TEXT,
                photo_file_id TEXT,
                schedule_time TEXT,
                is_cyclic INTEGER DEFAULT 0,
                cycle_days INTEGER DEFAULT 0,
                target_subscribers INTEGER DEFAULT 1,
                created_at INTEGER,
                last_sent INTEGER,
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                payment_id TEXT,
                amount INTEGER,
                status TEXT,
                created_at INTEGER
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await db.commit()

# ======================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def get_user_subscription(user_id: int) -> Optional[int]:
    async with aiosqlite.connect('bot.db') as db:
        async with db.execute(
            'SELECT subscription_until FROM users WHERE user_id = ?', 
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def has_active_subscription(user_id: int) -> bool:
    sub_until = await get_user_subscription(user_id)
    if not sub_until:
        return False
    return sub_until > int(datetime.now().timestamp())

async def get_days_left(user_id: int) -> int:
    sub_until = await get_user_subscription(user_id)
    if not sub_until or sub_until <= int(datetime.now().timestamp()):
        return 0
    days = (sub_until - int(datetime.now().timestamp())) / (60 * 60 * 24)
    return max(0, int(days))

async def add_user(user_id: int, username: str = None, phone: str = None):
    async with aiosqlite.connect('bot.db') as db:
        await db.execute('''
            INSERT OR IGNORE INTO users (user_id, username, phone, created_at)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, phone, int(datetime.now().timestamp())))
        await db.commit()

async def update_phone(user_id: int, phone: str):
    async with aiosqlite.connect('bot.db') as db:
        await db.execute(
            'UPDATE users SET phone = ? WHERE user_id = ?',
            (phone, user_id)
        )
        await db.commit()

async def activate_subscription(user_id: int, days: int = 30):
    sub_until = int((datetime.now() + timedelta(days=days)).timestamp())
    async with aiosqlite.connect('bot.db') as db:
        await db.execute(
            'UPDATE users SET subscription_until = ? WHERE user_id = ?',
            (sub_until, user_id)
        )
        await db.commit()
    
    # Добавляем в канал
    try:
        await bot.unban_chat_member(CHANNEL_ID, user_id)
        # Создаем инвайт-ссылку для пользователя
        invite_link = await bot.create_chat_invite_link(
            CHANNEL_ID,
            member_limit=1
        )
        return invite_link.invite_link
    except Exception as e:
        logging.error(f"Ошибка добавления в канал: {e}")
        return None

async def remove_from_channel_db(user_id: int):
    try:
        await bot.ban_chat_member(CHANNEL_ID, user_id)
    except Exception as e:
        logging.error(f"Ошибка удаления из канала: {e}")

async def get_welcome_photo() -> Optional[str]:
    """Получить file_id приветственного фото из базы"""
    async with aiosqlite.connect('bot.db') as db:
        async with db.execute(
            'SELECT value FROM settings WHERE key = ?',
            ('welcome_photo',)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_welcome_photo(file_id: str):
    """Сохранить file_id приветственного фото"""
    async with aiosqlite.connect('bot.db') as db:
        await db.execute(
            'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
            ('welcome_photo', file_id)
        )
        await db.commit()

# ======================== КЛАВИАТУРЫ ========================
def main_keyboard() -> ReplyKeyboardMarkup:
    """Основная клавиатура в нижней части экрана"""
    buttons = [
        [KeyboardButton(text="🔐 Оплатить доступ")],
        [KeyboardButton(text="✅ Активная подписка")],
        [KeyboardButton(text="💬 Поддержка")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def admin_main_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура для админа с кнопкой админ-панели"""
    buttons = [
        [KeyboardButton(text="🔐 Оплатить доступ"), KeyboardButton(text="✅ Активная подписка")],
        [KeyboardButton(text="💬 Поддержка"), KeyboardButton(text="⚙️ Админ-панель")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def admin_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📅 Запланированные", callback_data="admin_scheduled")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="🖼 Изменить приветственное фото", callback_data="admin_change_photo")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def tariff_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📅 Тариф на 1 месяц", callback_data="tariff_1")],
        [InlineKeyboardButton(text="« Назад", callback_data="back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    import calendar
    cal = calendar.monthcalendar(year, month)
    month_names = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                   'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']
    
    buttons = [[InlineKeyboardButton(text=f"📅 {month_names[month-1]} {year}", callback_data="ignore")]]
    buttons.append([InlineKeyboardButton(text=day, callback_data="ignore") 
                    for day in ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']])
    
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                row.append(InlineKeyboardButton(
                    text=str(day),
                    callback_data=f"day_{year}_{month}_{day}"
                ))
        buttons.append(row)
    
    nav_row = []
    if month > 1:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"cal_{year}_{month-1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"cal_{year-1}_12"))
    
    nav_row.append(InlineKeyboardButton(text="« Отмена", callback_data="cancel_broadcast"))
    
    if month < 12:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"cal_{year}_{month+1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"cal_{year+1}_1"))
    
    buttons.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def time_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for hour in range(0, 24, 3):
        row = []
        for h in range(hour, min(hour + 3, 24)):
            row.append(InlineKeyboardButton(
                text=f"{h:02d}:00",
                callback_data=f"time_{h}_0"
            ))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="cancel_broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="🔄 Циклическая (каждый день)", callback_data="cycle_1"),
            InlineKeyboardButton(text="🔄 Каждые 2 дня", callback_data="cycle_2")
        ],
        [
            InlineKeyboardButton(text="🔄 Каждые 3 дня", callback_data="cycle_3"),
            InlineKeyboardButton(text="📅 Разовая", callback_data="cycle_0")
        ],
        [
            InlineKeyboardButton(text="✅ Только подписчики", callback_data="target_1"),
            InlineKeyboardButton(text="👥 Всем", callback_data="target_0")
        ],
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_broadcast"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ======================== ОБРАБОТЧИКИ КОМАНД ========================
@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name or "друг"
    
    await add_user(user_id, username)
    
    welcome_text = f"""<i>{first_name}</i>, приветствую, если ты здесь, значит тебе интересно познать технику, систему и прогресс!

Здесь не нужно жить спортом — достаточно 2 занятий в неделю, чтобы тело слушалось, а техника становилась лучше.

<b>Нет времени на тренировки</b> — отлично.

Мы учимся вписывать движение в жизнь так, чтобы оно давало энергию, а не отнимало её.

<b>Думаешь нагрузка слишком большая?</b>
Всё адаптировано под любителей.
Тренировки делаются в комфортном темпе, с возможностью выполнять их в записи.

<b>Думаешь, система не нужна?</b>
Вспомни катание "на ощущениях": то летишь, то лень. Система — не про строгость, а про стабильность.

А если ты катаешься "для себя" — тем более тебе сюда. Потому что кататься для себя можно по-разному:
 ❌ страдая и уставая 
 ✅ или легко, технично и в удовольствии.

<b>Что внутри:</b> 
• Тренировочные планы
• Прямые эфиры и записи занятий
• Ответы на твои вопросы
• Раз в месяц — тренировка с тренером сборной России Алексеем Торицыным

Добро пожаловать.
Твоя система уже ждёт тебя.

После оплаты вышлю доступ в канал."""
    
    keyboard = admin_main_keyboard() if is_admin(user_id) else main_keyboard()
    
    # Получаем приветственное фото из базы
    welcome_photo_id = await get_welcome_photo()
    
    if welcome_photo_id:
        # Если фото есть в базе, отправляем его
        try:
            await message.answer_photo(
                photo=welcome_photo_id,
                caption=welcome_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Ошибка отправки фото из базы: {e}")
            # Если не получилось отправить фото из базы, отправляем текст
            await message.answer(
                welcome_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
    else:
        # Если фото нет в базе, пытаемся загрузить из файла
        try:
            with open(WELCOME_PHOTO, 'rb') as photo:
                msg = await message.answer_photo(
                    photo=photo,
                    caption=welcome_text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
                # Сохраняем file_id в базу для будущего использования
                if msg.photo:
                    await set_welcome_photo(msg.photo[-1].file_id)
        except FileNotFoundError:
            # Если файла нет, отправляем просто текст
            await message.answer(
                welcome_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
    
    if is_admin(user_id):
        await message.answer("🔑 <b>Админ-панель доступна через кнопку ⚙️ Админ-панель</b>", parse_mode="HTML")

# ======================== ОБРАБОТЧИКИ КНОПОК КЛАВИАТУРЫ ========================
@router.message(F.text == "🔐 Оплатить доступ")
async def pay_button(message: Message):
    text = "🔐 <b>Тариф для:</b> 'Кафедра любительского спорта'\n\n"
    text += "При оплате тарифа Вы получите доступ: <i>Кафедра любительского спорта</i>"
    
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=tariff_keyboard()
    )

@router.message(F.text == "✅ Активная подписка")
async def subscription_button(message: Message):
    user_id = message.from_user.id
    
    if await has_active_subscription(user_id):
        days = await get_days_left(user_id)
        text = f"✅ <b>Активная подписка</b>\n\n"
        text += f"Осталось дней: <b>{days}</b>"
    else:
        text = "❌ <b>Нет активной подписки</b>\n\n"
        text += "Оформите подписку для доступа к материалам."
    
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "💬 Поддержка")
async def support_button(message: Message):
    await message.answer(
        "💬 <b>Поддержка:</b>\n\n"
        "Свяжитесь с нами: @pavlychevayana99",
        parse_mode="HTML"
    )

@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel_button(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к админ-панели")
        return
    
    await message.answer("🔑 Админ-панель:", reply_markup=admin_keyboard())

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к админ-панели")
        return
    
    await message.answer("🔑 Админ-панель:", reply_markup=admin_keyboard())

# ======================== ОБРАБОТЧИКИ CALLBACK ========================
@router.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@router.callback_query(F.data == "tariff_1")
async def tariff_1_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📱 Для отслеживания подписки отправьте свой номер телефона:",
        reply_markup=phone_keyboard()
    )
    await state.set_state(PhoneState.waiting_phone)
    await callback.answer()

@router.message(PhoneState.waiting_phone, F.contact)
async def phone_received(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    user_id = message.from_user.id
    
    await update_phone(user_id, phone)
    
    keyboard = admin_main_keyboard() if is_admin(user_id) else main_keyboard()
    
    # Создание платежа через ЮКassa
    try:
        payment = Payment.create({
            "amount": {
                "value": f"{PRICE_1_MONTH}.00",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{(await bot.get_me()).username}"
            },
            "capture": True,
            "description": "Подписка на 1 месяц - Кафедра любительского спорта",
            "metadata": {
                "user_id": user_id
            }
        }, uuid.uuid4())
        
        async with aiosqlite.connect('bot.db') as db:
            await db.execute(
                'INSERT INTO payments (user_id, payment_id, amount, status, created_at) VALUES (?, ?, ?, ?, ?)',
                (user_id, payment.id, PRICE_1_MONTH, 'pending', int(datetime.now().timestamp()))
            )
            await db.commit()
        
        buttons = [[InlineKeyboardButton(text="💳 Оплатить", url=payment.confirmation.confirmation_url)]]
        await message.answer(
            f"💰 Счет на оплату создан!\n\n"
            f"Сумма: {PRICE_1_MONTH} ₽\n"
            f"Нажмите кнопку ниже для оплаты:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        
        await message.answer(
            "⬆️ Нажмите на кнопку оплаты выше",
            reply_markup=keyboard
        )
        
        asyncio.create_task(check_payment(payment.id, user_id))
        
    except Exception as e:
        logging.error(f"Ошибка создания платежа: {e}")
        await message.answer("❌ Ошибка создания платежа. Попробуйте позже.", reply_markup=keyboard)
    
    await state.clear()

async def check_payment(payment_id: str, user_id: int, max_checks: int = 60):
    """Проверка статуса платежа"""
    for _ in range(max_checks):
        await asyncio.sleep(10)
        try:
            payment = Payment.find_one(payment_id)
            if payment.status == 'succeeded':
                invite_link = await activate_subscription(user_id, 30)
                
                async with aiosqlite.connect('bot.db') as db:
                    await db.execute(
                        'UPDATE payments SET status = ? WHERE payment_id = ?',
                        ('succeeded', payment_id)
                    )
                    await db.commit()
                
                msg = "✅ <b>Оплата прошла успешно!</b>\n\n" \
                      "Подписка активирована на 30 дней.\n"
                
                if invite_link:
                    msg += f"\n🔗 Ссылка для входа в канал:\n{invite_link}"
                else:
                    msg += "\nДобро пожаловать в канал!"
                
                await bot.send_message(user_id, msg, parse_mode="HTML")
                break
        except Exception as e:
            logging.error(f"Ошибка проверки платежа: {e}")

# ======================== АДМИН ПАНЕЛЬ ========================
@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    async with aiosqlite.connect('bot.db') as db:
        cursor = await db.execute('SELECT COUNT(*) FROM users')
        total = (await cursor.fetchone())[0]
        
        cursor = await db.execute(
            'SELECT COUNT(*) FROM users WHERE subscription_until > ?',
            (int(datetime.now().timestamp()),)
        )
        active = (await cursor.fetchone())[0]
        
        cursor = await db.execute(
            'SELECT SUM(amount) FROM payments WHERE status = "succeeded"'
        )
        revenue = (await cursor.fetchone())[0] or 0
    
    text = f"📊 <b>Статистика бота</b>\n\n"
    text += f"👥 Всего пользователей: <b>{total}</b>\n"
    text += f"✅ Активных подписок: <b>{active}</b>\n"
    text += f"💰 Общая выручка: <b>{revenue} ₽</b>"
    
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    async with aiosqlite.connect('bot.db') as db:
        async with db.execute(
            'SELECT user_id, username, phone, subscription_until FROM users ORDER BY created_at DESC LIMIT 20'
        ) as cursor:
            users = await cursor.fetchall()
    
    text = "👥 <b>Последние пользователи:</b>\n\n"
    for user_id, username, phone, sub_until in users:
        status = "✅" if sub_until and sub_until > int(datetime.now().timestamp()) else "❌"
        username_str = f"@{username}" if username else f"ID: {user_id}"
        phone_str = f"📱 {phone}" if phone else "Нет номера"
        text += f"{status} {username_str}\n{phone_str}\n\n"
    
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.answer(
        "📝 Отправьте текст или фото с подписью для рассылки:",
    )
    await state.set_state(BroadcastStates.waiting_message)
    await callback.answer()

@router.message(BroadcastStates.waiting_message)
async def broadcast_message_received(message: Message, state: FSMContext):
    # Проверяем, есть ли фото
    if message.photo:
        photo_file_id = message.photo[-1].file_id
        caption = message.caption or ""
        await state.update_data(message_text=caption, photo_file_id=photo_file_id)
    else:
        await state.update_data(message_text=message.text or "", photo_file_id=None)
    
    now = datetime.now()
    await message.answer(
        "📅 Выберите дату рассылки:",
        reply_markup=calendar_keyboard(now.year, now.month)
    )
    await state.set_state(BroadcastStates.selecting_date)

@router.callback_query(BroadcastStates.selecting_date, F.data.startswith("cal_"))
async def calendar_nav(callback: CallbackQuery):
    _, year, month = callback.data.split("_")
    await callback.message.edit_reply_markup(
        reply_markup=calendar_keyboard(int(year), int(month))
    )
    await callback.answer()

@router.callback_query(BroadcastStates.selecting_date, F.data.startswith("day_"))
async def day_selected(callback: CallbackQuery, state: FSMContext):
    _, year, month, day = callback.data.split("_")
    await state.update_data(year=year, month=month, day=day)
    
    await callback.message.edit_text(
        f"📅 Выбрана дата: {day}.{month}.{year}\n\n"
        "🕐 Выберите время:",
        reply_markup=time_keyboard()
    )
    await state.set_state(BroadcastStates.selecting_time)
    await callback.answer()

@router.callback_query(BroadcastStates.selecting_time, F.data.startswith("time_"))
async def time_selected(callback: CallbackQuery, state: FSMContext):
    _, hour, minute = callback.data.split("_")
    data = await state.get_data()
    
    schedule_dt = datetime(
        int(data['year']), int(data['month']), int(data['day']),
        int(hour), int(minute)
    )
    
    await state.update_data(
        schedule_time=schedule_dt.isoformat(),
        is_cyclic=0,
        cycle_days=0,
        target_subscribers=1
    )
    
    data = await state.get_data()
    has_photo = "📷 Да" if data.get('photo_file_id') else "📝 Нет"
    
    text = f"📋 <b>Параметры рассылки:</b>\n\n"
    text += f"📝 Текст: {data['message_text'][:50] if data['message_text'] else 'Без текста'}...\n"
    text += f"📷 Фото: {has_photo}\n"
    text += f"📅 Дата: {schedule_dt.strftime('%d.%m.%Y %H:%M')}\n"
    text += f"🔄 Тип: <b>Разовая</b>\n"
    text += f"👥 Целевая аудитория: <b>Только подписчики</b>\n\n"
    text += "Настройте параметры или отправьте:"
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=broadcast_confirm_keyboard()
    )
    await state.set_state(BroadcastStates.confirming)
    await callback.answer()

@router.callback_query(BroadcastStates.confirming, F.data.startswith("cycle_"))
async def cycle_selected(callback: CallbackQuery, state: FSMContext):
    cycle_days = int(callback.data.split("_")[1])
    await state.update_data(
        is_cyclic=1 if cycle_days > 0 else 0,
        cycle_days=cycle_days
    )
    
    data = await state.get_data()
    cycle_text = "Разовая" if cycle_days == 0 else f"Каждые {cycle_days} дн."
    has_photo = "📷 Да" if data.get('photo_file_id') else "📝 Нет"
    
    schedule_dt = datetime.fromisoformat(data['schedule_time'])
    
    text = f"📋 <b>Параметры рассылки:</b>\n\n"
    text += f"📝 Текст: {data['message_text'][:50] if data.get('message_text') else 'Без текста'}...\n"
    text += f"📷 Фото: {has_photo}\n"
    text += f"📅 Дата: {schedule_dt.strftime('%d.%m.%Y %H:%M')}\n"
    text += f"🔄 Тип: <b>{cycle_text}</b>\n"
    text += f"👥 Целевая аудитория: <b>{'Только подписчики' if data['target_subscribers'] else 'Всем'}</b>"
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=broadcast_confirm_keyboard()
    )
    await callback.answer("✅ Тип рассылки изменен")

@router.callback_query(BroadcastStates.confirming, F.data.startswith("target_"))
async def target_selected(callback: CallbackQuery, state: FSMContext):
    target = int(callback.data.split("_")[1])
    await state.update_data(target_subscribers=target)
    
    data = await state.get_data()
    cycle_text = "Разовая" if data['cycle_days'] == 0 else f"Каждые {data['cycle_days']} дн."
    has_photo = "📷 Да" if data.get('photo_file_id') else "📝 Нет"
    
    schedule_dt = datetime.fromisoformat(data['schedule_time'])
    
    text = f"📋 <b>Параметры рассылки:</b>\n\n"
    text += f"📝 Текст: {data['message_text'][:50] if data.get('message_text') else 'Без текста'}...\n"
    text += f"📷 Фото: {has_photo}\n"
    text += f"📅 Дата: {schedule_dt.strftime('%d.%m.%Y %H:%M')}\n"
    text += f"🔄 Тип: <b>{cycle_text}</b>\n"
    text += f"👥 Целевая аудитория: <b>{'Только подписчики' if target else 'Всем'}</b>"
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=broadcast_confirm_keyboard()
    )
    await callback.answer("✅ Целевая аудитория изменена")

@router.callback_query(BroadcastStates.confirming, F.data == "confirm_broadcast")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    data = await state.get_data()
    
    async with aiosqlite.connect('bot.db') as db:
        await db.execute('''
            INSERT INTO broadcasts 
            (message_text, photo_file_id, schedule_time, is_cyclic, cycle_days, target_subscribers, created_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ''', (
            data['message_text'],
            data.get('photo_file_id'),
            data['schedule_time'],
            data['is_cyclic'],
            data['cycle_days'],
            data['target_subscribers'],
            int(datetime.now().timestamp())
        ))
        await db.commit()
    
    await callback.message.edit_text(
        "✅ Рассылка запланирована!\n\n"
        f"📅 Будет отправлена: {data['schedule_time']}"
    )
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("❌ Создание рассылки отменено")
    await callback.answer()

@router.callback_query(F.data == "admin_scheduled")
async def admin_scheduled(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    async with aiosqlite.connect('bot.db') as db:
        async with db.execute(
            'SELECT id, message_text, photo_file_id, schedule_time, is_cyclic, cycle_days FROM broadcasts WHERE is_active = 1'
        ) as cursor:
            broadcasts = await cursor.fetchall()
    
    if not broadcasts:
        await callback.message.answer("📭 Нет запланированных рассылок")
        await callback.answer()
        return
    
    text = "📅 <b>Запланированные рассылки:</b>\n\n"
    for bc_id, msg, photo, sched_time, is_cyclic, cycle_days in broadcasts:
        cycle_text = f"🔄 Каждые {cycle_days} дн." if is_cyclic else "📅 Разовая"
        photo_text = "📷 с фото" if photo else ""
        text += f"<b>ID {bc_id}</b> | {cycle_text} {photo_text}\n"
        text += f"📝 {msg[:30] if msg else 'Без текста'}...\n"
        text += f"🕐 {sched_time}\n\n"
    
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()

# ======================== ИЗМЕНЕНИЕ ПРИВЕТСТВЕННОГО ФОТО ========================
@router.callback_query(F.data == "admin_change_photo")
async def admin_change_photo(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.answer(
        "🖼 <b>Изменение приветственного фото</b>\n\n"
        "Отправьте новое фото, которое будет показываться при команде /start",
        parse_mode="HTML"
    )
    await state.set_state(WelcomePhotoState.waiting_photo)
    await callback.answer()

@router.message(WelcomePhotoState.waiting_photo, F.photo)
async def welcome_photo_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа")
        await state.clear()
        return
    
    # Получаем file_id фото в лучшем качестве
    photo_file_id = message.photo[-1].file_id
    
    # Сохраняем в базу
    await set_welcome_photo(photo_file_id)
    
    await message.answer(
        "✅ <b>Приветственное фото успешно обновлено!</b>\n\n"
        "Теперь все новые пользователи будут видеть это фото при команде /start",
        parse_mode="HTML"
    )
    await state.clear()

@router.message(WelcomePhotoState.waiting_photo)
async def welcome_photo_invalid(message: Message, state: FSMContext):
    await message.answer(
        "❌ Пожалуйста, отправьте фото (не файл, не документ)\n\n"
        "Или отправьте /cancel для отмены"
    )

@router.message(Command("cancel"), StateFilter(WelcomePhotoState.waiting_photo))
async def cancel_photo_change(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Изменение фото отменено")

# ======================== ФОНОВЫЕ ЗАДАЧИ ========================
async def check_subscriptions():
    """Проверка истекших подписок каждый час"""
    while True:
        try:
            async with aiosqlite.connect('bot.db') as db:
                async with db.execute(
                    'SELECT user_id FROM users WHERE subscription_until <= ? AND subscription_until > 0',
                    (int(datetime.now().timestamp()),)
                ) as cursor:
                    expired = await cursor.fetchall()
                    
                    for (user_id,) in expired:
                        await remove_from_channel_db(user_id)
                        try:
                            await bot.send_message(
                                user_id,
                                "⏰ Ваша подписка истекла.\n"
                                "Продлите подписку для доступа к материалам."
                            )
                        except:
                            pass
                
                await db.execute(
                    'UPDATE users SET subscription_until = 0 WHERE subscription_until <= ? AND subscription_until > 0',
                    (int(datetime.now().timestamp()),)
                )
                await db.commit()
        except Exception as e:
            logging.error(f"Ошибка проверки подписок: {e}")
        
        await asyncio.sleep(3600)

async def check_broadcasts():
    """Проверка и отправка запланированных рассылок"""
    while True:
        try:
            now = datetime.now()
            
            async with aiosqlite.connect('bot.db') as db:
                async with db.execute('''
                    SELECT id, message_text, photo_file_id, schedule_time, is_cyclic, cycle_days, target_subscribers, last_sent
                    FROM broadcasts WHERE is_active = 1
                ''') as cursor:
                    broadcasts = await cursor.fetchall()
                
                for bc_id, msg, photo, sched_time, is_cyclic, cycle_days, target_subs, last_sent in broadcasts:
                    schedule_dt = datetime.fromisoformat(sched_time)
                    
                    should_send = False
                    
                    if is_cyclic and last_sent:
                        last_sent_dt = datetime.fromtimestamp(last_sent)
                        days_passed = (now - last_sent_dt).days
                        if days_passed >= cycle_days and now >= schedule_dt:
                            should_send = True
                    elif not is_cyclic and now >= schedule_dt and not last_sent:
                        should_send = True
                    
                    if should_send:
                        if target_subs:
                            query = 'SELECT user_id FROM users WHERE subscription_until > ?'
                            params = (int(now.timestamp()),)
                        else:
                            query = 'SELECT user_id FROM users'
                            params = ()
                        
                        async with db.execute(query, params) as user_cursor:
                            users = await user_cursor.fetchall()
                        
                        sent_count = 0
                        for (user_id,) in users:
                            try:
                                if photo:
                                    await bot.send_photo(user_id, photo, caption=msg)
                                else:
                                    await bot.send_message(user_id, msg)
                                sent_count += 1
                                await asyncio.sleep(0.05)
                            except:
                                pass
                        
                        await db.execute(
                            'UPDATE broadcasts SET last_sent = ? WHERE id = ?',
                            (int(now.timestamp()), bc_id)
                        )
                        
                        if not is_cyclic:
                            await db.execute(
                                'UPDATE broadcasts SET is_active = 0 WHERE id = ?',
                                (bc_id,)
                            )
                        
                        await db.commit()
                        
                        logging.info(f"Рассылка {bc_id} отправлена {sent_count} пользователям")
        
        except Exception as e:
            logging.error(f"Ошибка в рассылке: {e}")
        
        await asyncio.sleep(60)

# ======================== ЗАПУСК БОТА ========================
async def main():
    await init_db()
    dp.include_router(router)
    
    asyncio.create_task(check_subscriptions())
    asyncio.create_task(check_broadcasts())
    
    logging.info("🚀 Бот запущен!")
    logging.info(f"👤 Админы: {ADMIN_IDS}")
    logging.info(f"📢 ID канала: {CHANNEL_ID}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())