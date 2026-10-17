"""
Неотвеченные чаты (ChatGuard) — плагин для FunPay Cardinal v0.1.17+

⏱ Следит, чтобы покупатели не оставались без ответа: если сообщение покупателя
висит без ответа дольше N минут — присылает тревожное уведомление в Telegram.

Возможности:
 • Отслеживает все чаты: сообщение от покупателя → таймер; ваш ответ (с сайта,
   из приложения или от бота Кардинала) → таймер снимается.
 • ⏱ Порог ожидания настраивается (по умолчанию 30 минут).
 • 🔁 Повторные напоминания (опционально): каждые X минут, пока чат не отвечен.
 • Системные сообщения FunPay («заказ подтвержден», «новый отзыв», возвраты и т.п.)
   игнорируются — трекаются только живые сообщения покупателей и «оплатил заказ».
 • Состояние переживает рестарт Кардинала (отсчет идет от момента обнаружения сообщения).
 • Меню /chatguard: список ждущих чатов с возрастом, настройки, кнопка «очистить всё».

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.common.enums import MessageTypes

logger = logging.getLogger("FPC.ChatGuard")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "Неотвеченные чаты (ChatGuard)"
VERSION = "1.0.0"
DESCRIPTION = ("Тревожные уведомления, если покупатель ждёт ответа дольше N минут. "
               "Порог и повторные напоминания настраиваются, системные сообщения игнорируются. "
               "Команда: /chatguard")
CREDITS = "Qwen (для FunPay Cardinal)"
UUID = "d1ad3c05-9c4a-46c0-b2d7-f6b466f03342"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "chat_guard")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "enabled": True,     # мониторинг вкл/выкл
    "threshold": 30,     # порог ожидания, минут
    "re_alert": 0,       # повторные напоминания каждые X минут (0 = только один раз)
    "chats": {},         # {str(chat_id): {"ts": float, "name": str, "buyer": str, "buyer_id": int,
                         #  "text": str, "alerted_ts": float}}
}
THRESHOLD_RANGE = (1, 1440)     # минут
RE_ALERT_RANGE = (0, 1440)      # минут, 0 = выкл
LOOP_SLEEP = 20                 # сек, период проверки
TRACKED_TYPES = (None, MessageTypes.NON_SYSTEM, MessageTypes.ORDER_PURCHASED)

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("ChatGuard: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        settings["enabled"] = bool(settings["enabled"])
        for key, rng in [("threshold", THRESHOLD_RANGE), ("re_alert", RE_ALERT_RANGE)]:
            try:
                settings[key] = _clamp(int(settings[key]), *rng)
            except (TypeError, ValueError):
                settings[key] = DEFAULT_SETTINGS[key]
        if not isinstance(settings["chats"], dict):
            settings["chats"] = {}
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("ChatGuard: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _human_mins(seconds: float) -> str:
    m = int(max(0, seconds) // 60)
    if m < 60:
        return f"{m} мин."
    return f"{m // 60} ч. {m % 60} мин."


def _notify(crd: "Cardinal", text: str) -> None:
    if not crd.telegram:
        return
    kwargs = {"notification_type": tg_utils.NotificationTypes.other} if tg_utils else {}
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════════ ОТСЛЕЖИВАНИЕ ЧАТОВ ══════════════════════════════
def new_message_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_NEW_MESSAGE: отмечает сообщения покупателей и снимает отметки при ответе."""
    try:
        if not _plugin_enabled(c):
            return
        settings = _load_settings()
        if not settings["enabled"]:
            return
        msg = getattr(e, "message", None)
        if msg is None:
            return
        chat_key = str(msg.chat_id)
        my_id = getattr(c.account, "id", None)

        # мой ответ (с сайта/приложения) или сообщение бота Кардинала → чат отвечен
        if getattr(msg, "by_bot", False) or (my_id is not None and msg.author_id == my_id):
            if chat_key in settings["chats"]:
                del settings["chats"][chat_key]
                _save_settings(settings)
                logger.info(f"ChatGuard: чат «{msg.chat_name}» отвечен — таймер снят.")
            return

        # системные сообщения FunPay (author_id == 0) — игнорируем
        if msg.author_id == 0:
            return

        # информационные системные типы (подтверждение заказа, отзывы, возвраты) — игнорируем
        if getattr(msg, "type", None) not in TRACKED_TYPES:
            return

        # сообщение от покупателя → ставим/обновляем таймер ожидания
        entry = settings["chats"].get(chat_key)
        if entry is None:
            settings["chats"][chat_key] = {
                "ts": time.time(),
                "name": str(msg.chat_name or msg.author or chat_key),
                "buyer": str(msg.author or "?"),
                "buyer_id": int(msg.author_id) if msg.author_id else 0,
                "text": _short(msg.text or "(фото/медиа)", 120),
                "alerted_ts": 0.0,
            }
            _save_settings(settings)
            logger.debug(f"ChatGuard: чат «{msg.chat_name}» взят на контроль (ждет ответа).")
        else:
            # обновляем превью последнего сообщения, но НЕ сбрасываем таймер ожидания
            entry["text"] = _short(msg.text or "(фото/медиа)", 120)
            entry["buyer"] = str(msg.author or entry.get("buyer", "?"))
            _save_settings(settings)
    except Exception:
        logger.error("ChatGuard: ошибка в обработчике нового сообщения.")
        logger.debug("TRACEBACK", exc_info=True)


def _guard_tick(crd: "Cardinal", now_ts: float | None = None) -> list:
    """
    Один проход проверки: шлет уведомления о чатах, где покупатель ждет дольше порога.
    Возвращает список chat_id, по которым отправлены уведомления (для тестируемости).
    """
    settings = _load_settings()
    if not settings["enabled"] or not settings["chats"]:
        return []
    now_ts = now_ts if now_ts is not None else time.time()
    threshold_s = settings["threshold"] * 60
    re_alert_s = settings["re_alert"] * 60
    alerted = []
    for chat_key, entry in list(settings["chats"].items()):
        try:
            waited = now_ts - float(entry.get("ts", now_ts))
            if waited < threshold_s:
                continue
            last_alert = float(entry.get("alerted_ts", 0) or 0)
            if last_alert and not (re_alert_s and now_ts - last_alert >= re_alert_s):
                continue  # уже напомнили, повторы выключены или еще рано
            buyer_id = entry.get("buyer_id", 0)
            buyer_line = (f"<a href=\"https://funpay.com/users/{buyer_id}/\">{_escape(entry.get('buyer'))}</a>"
                          if buyer_id else _escape(entry.get("buyer")))
            text = (f"⏱ <b>Покупатель ждёт ответа уже {_human_mins(waited)}!</b>\n\n"
                    f"Чат: <b>{_escape(_short(entry.get('name'), 60))}</b>\n"
                    f"Покупатель: {buyer_line}\n"
                    f"Сообщение: <code>{_escape(entry.get('text', ''))}</code>")
            if re_alert_s:
                text += f"\n\n<i>Напомню повторно через {_human_mins(re_alert_s)}, пока не ответите.</i>"
            _notify(crd, text)
            entry["alerted_ts"] = now_ts
            alerted.append(chat_key)
            logger.info(f"ChatGuard: напомнил о чате «{entry.get('name')}» (ждет {_human_mins(waited)}).")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
    if alerted:
        _save_settings(settings)
    return alerted


def _guard_loop(crd: "Cardinal") -> None:
    logger.info("ChatGuard: мониторинг неотвеченных чатов запущен.")
    while not _stop_event.wait(LOOP_SLEEP):
        try:
            if not _plugin_enabled(crd):
                continue
            _guard_tick(crd)
        except Exception:
            logger.error("ChatGuard: ошибка в цикле проверки.")
            logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
CB_HOME = "cg:home"
CB_TOG = "cg:tog"
CB_THR = "cg:thr"
CB_RE = "cg:re"
CB_CLEAR = "cg:clear"
CB_CLEAR_Y = "cg:cleary"
CB_NOOP = "cg:noop"
ST_THR = "cg:st:thr"
ST_RE = "cg:st:re"


def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    chats = settings["chats"]
    now = time.time()
    text = (f"⏱ <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Мониторинг: <b>{'ВКЛ 🟢' if settings['enabled'] else 'ВЫКЛ 🔴'}</b>\n"
            f"Порог ожидания: <b>{settings['threshold']} мин.</b>\n"
            f"Повторные напоминания: <b>{'каждые ' + str(settings['re_alert']) + ' мин.' if settings['re_alert'] else 'выключены (одно напоминание)'}</b>\n\n")
    if chats:
        text += f"🔔 <b>Сейчас ждут ответа ({len(chats)}):</b>\n"
        items = sorted(chats.items(), key=lambda kv: kv[1].get("ts", 0))
        for chat_key, entry in items[:10]:
            waited = now - float(entry.get("ts", now))
            over = "🚨" if waited >= settings["threshold"] * 60 else "🕐"
            text += f"{over} {_escape(_short(entry.get('name'), 35))} — {_human_mins(waited)}\n"
        if len(chats) > 10:
            text += f"…и еще {len(chats) - 10}\n"
    else:
        text += "✅ Все чаты отвечены — никто не ждёт."
    return text


def _home_kb() -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    kb.add(B(f"{'🟢 Мониторинг: ВКЛ' if settings['enabled'] else '🔴 Мониторинг: ВЫКЛ'}", None, CB_TOG))
    kb.add(B(f"⏳ Порог: {settings['threshold']} мин.", None, CB_THR))
    re_txt = f"🔁 Повтор: {settings['re_alert']} мин." if settings["re_alert"] else "🔁 Повтор: ВЫКЛ"
    kb.add(B(re_txt, None, CB_RE))
    kb.add(B(f"🧹 Очистить список ({len(settings['chats'])})", None, CB_CLEAR))
    kb.add(B("🔄 Обновить", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def toggle_enabled(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["enabled"] = not settings["enabled"]
            _save_settings(settings)
            _answer(bot, c, f"⏱ Мониторинг: {'ВКЛ' if settings['enabled'] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def act_threshold(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"⏳ Введите порог ожидания ответа в минутах "
                f"(<code>{THRESHOLD_RANGE[0]}</code> - <code>{THRESHOLD_RANGE[1]}</code>).\n\n"
                f"Текущее значение: <b>{s['threshold']}</b> мин.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_THR)
            _answer(bot, c)

        def act_re_alert(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"🔁 Введите интервал повторных напоминаний в минутах "
                f"(<code>{RE_ALERT_RANGE[0]}</code> - <code>{RE_ALERT_RANGE[1]}</code>).\n\n"
                f"<code>0</code> — напоминать только один раз.\n"
                f"Текущее значение: <b>{s['re_alert']}</b>.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_RE)
            _answer(bot, c)

        def ask_clear(c: CallbackQuery) -> None:
            _answer(bot, c)
            kb = K(row_width=2).add(B("✅ Очистить", None, CB_CLEAR_Y), B("❌ Отмена", None, CB_HOME))
            _edit_or_send(bot, c, "🧹 Очистить список ждущих чатов?\n\n"
                                  "<i>Таймеры всех чатов будут сброшены (новые сообщения "
                                  "возьмутся на контроль заново).</i>", kb)

        def do_clear(c: CallbackQuery) -> None:
            settings = _load_settings()
            n = len(settings["chats"])
            settings["chats"] = {}
            _save_settings(settings)
            _answer(bot, c, f"🧹 Очищено чатов: {n}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def edit_threshold(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int(m, "threshold", THRESHOLD_RANGE, "⏳ Порог ожидания")

        def edit_re_alert(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int(m, "re_alert", RE_ALERT_RANGE, "🔁 Интервал повторов")

        def _edit_int(m: Message, key: str, rng: tuple, label: str) -> None:
            try:
                value = _clamp(int(str(m.text).strip()), *rng)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число. Попробуйте еще раз.")
                return
            settings = _load_settings()
            settings[key] = value
            _save_settings(settings)
            suffix = " (повторные напоминания выключены)" if key == "re_alert" and value == 0 else ""
            bot.reply_to(m, f"✅ {label}: <b>{value}</b> мин.{suffix}", parse_mode="HTML")

        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(toggle_enabled, lambda c: c.data == CB_TOG)
        tg.cbq_handler(act_threshold, lambda c: c.data == CB_THR)
        tg.cbq_handler(act_re_alert, lambda c: c.data == CB_RE)
        tg.cbq_handler(ask_clear, lambda c: c.data == CB_CLEAR)
        tg.cbq_handler(do_clear, lambda c: c.data == CB_CLEAR_Y)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_threshold, func=lambda m: _state_eq(m, ST_THR))
        tg.msg_handler(edit_re_alert, func=lambda m: _state_eq(m, ST_RE))
        tg.msg_handler(cmd_open, commands=["chatguard"])

        try:
            crd.add_telegram_commands(UUID, [("chatguard", "Мониторинг неотвеченных чатов.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"ChatGuard v{VERSION}: Telegram-меню зарегистрировано (команда /chatguard).")
    else:
        logger.info(f"ChatGuard v{VERSION}: Telegram выключен — мониторинг работает в фоновом режиме.")

    threading.Thread(target=_guard_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [new_message_handler]
BIND_TO_DELETE = on_delete
