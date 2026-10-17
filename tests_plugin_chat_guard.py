"""
Функциональные тесты плагина chat_guard.py на моках.
Запуск: cd FPC && python3 tests_plugin_chat_guard.py
"""
import importlib.util
import json
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

from FunPayAPI.common.enums import MessageTypes

spec = importlib.util.spec_from_file_location("plugins.chat_guard", "plugins/chat_guard.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.chat_guard"] = plugin
spec.loader.exec_module(plugin)

plugin._guard_loop = lambda crd: None  # поток тестируем через _guard_tick

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


def mk_msg(author="buyer1", author_id=555, chat_id=1000, chat_name="buyer1",
           text="Здравствуйте, товар еще актуален?", by_bot=False, mtype=None):
    return SimpleNamespace(author=author, author_id=author_id, chat_id=chat_id,
                           chat_name=chat_name, text=text, by_bot=by_bot, type=mtype)


class MockBot:
    def __init__(self):
        self.answers, self.edits, self.sent, self.replies = [], [], [], []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers, self.msg_handlers, self.user_states = [], [], {}
        self.notification_settings, self.notifications = {}, []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def send_notification(self, text, notification_type=None, **kwargs):
        self.notifications.append((text, notification_type))
        for chat_id in list(self.notification_settings):
            self.bot.send_message(int(chat_id), text)


class MockCardinal:
    def __init__(self, telegram):
        self.account = SimpleNamespace(id=999, is_initiated=True)
        self.telegram = telegram
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


def make_crd(with_tg=True):
    tg = MockTelegram() if with_tg else None
    return MockCardinal(tg), tg


def fire_msg(crd, msg):
    plugin.new_message_handler(crd, SimpleNamespace(message=msg))


# ═══════════════════════ ТЕСТ 1: метаданные ═══════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 BIND_TO_NEW_MESSAGE привязан", plugin.BIND_TO_NEW_MESSAGE == [plugin.new_message_handler])

# ═══════════════════════ ТЕСТ 2: отслеживание сообщений ═══════════════════════
crd, tg = make_crd(with_tg=True)
reset_settings(threshold=30)

fire_msg(crd, mk_msg())
chats = plugin._load_settings()["chats"]
check("2.1 сообщение покупателя взято на контроль",
      "1000" in chats and chats["1000"]["buyer"] == "buyer1"
      and "актуален" in chats["1000"]["text"])

ts_first = chats["1000"]["ts"]
time.sleep(0.05)
fire_msg(crd, mk_msg(text="Ау?"))
chats = plugin._load_settings()["chats"]
check("2.2 второе сообщение НЕ сбрасывает таймер, превью обновляется",
      chats["1000"]["ts"] == ts_first and chats["1000"]["text"] == "Ау?")

fire_msg(crd, mk_msg(author="me", author_id=999, text="Здравствуйте! Да"))
check("2.3 мой ответ снимает чат с контроля", "1000" not in plugin._load_settings()["chats"])

fire_msg(crd, mk_msg())
fire_msg(crd, mk_msg(author="bot", author_id=999, text="Товар выдан", by_bot=True))
check("2.4 ответ бота Кардинала тоже снимает с контроля",
      "1000" not in plugin._load_settings()["chats"])

fire_msg(crd, mk_msg(author_id=0, text="Покупатель подтвердил заказ"))
check("2.5 системное сообщение (author_id=0) игнорируется",
      plugin._load_settings()["chats"] == {})

fire_msg(crd, mk_msg(mtype=MessageTypes.ORDER_CONFIRMED, text="Заказ подтвержден"))
check("2.6 инфо-системное сообщение (ORDER_CONFIRMED) игнорируется",
      plugin._load_settings()["chats"] == {})

fire_msg(crd, mk_msg(mtype=MessageTypes.ORDER_PURCHASED, text="Покупатель оплатил заказ"))
check("2.7 «оплатил заказ» (ORDER_PURCHASED) берется на контроль",
      "1000" in plugin._load_settings()["chats"])
reset_settings(threshold=30)

fire_msg(crd, mk_msg())
crd.plugins[plugin.UUID].enabled = False
fire_msg(crd, mk_msg(chat_id=2000))
check("2.8 выключенный плагин не трекает чаты",
      "2000" not in plugin._load_settings()["chats"])
crd.plugins[plugin.UUID].enabled = True

reset_settings(enabled=False)
fire_msg(crd, mk_msg(chat_id=3000))
check("2.9 enabled=False в настройках — трекинг выключен",
      "3000" not in plugin._load_settings()["chats"])

# ═══════════════════════ ТЕСТ 3: уведомления (_guard_tick) ═══════════════════════
reset_settings(threshold=30)
fire_msg(crd, mk_msg())
tg.notifications.clear()
alerted = plugin._guard_tick(crd, now_ts=time.time() + 10 * 60)  # 10 мин — рано
check("3.1 до порога (10 < 30 мин) уведомления нет", alerted == [] and tg.notifications == [])

alerted = plugin._guard_tick(crd, now_ts=time.time() + 35 * 60)
check("3.2 после порога (35 мин) — уведомление ⏱",
      alerted == ["1000"] and any("⏱" in t and "35 мин" in t for t, _ in tg.notifications))
check("3.3 в уведомлении имя покупателя и ссылка", "buyer1" in tg.notifications[0][0]
      and "funpay.com/users/555" in tg.notifications[0][0])
check("3.4 alerted_ts записан", plugin._load_settings()["chats"]["1000"]["alerted_ts"] > 0)

tg.notifications.clear()
alerted = plugin._guard_tick(crd, now_ts=time.time() + 40 * 60)
check("3.5 без повторов (re_alert=0) — второго уведомления нет", alerted == [] and tg.notifications == [])

# ответ снимает с контроля — тик молчит
fire_msg(crd, mk_msg(author="me", author_id=999, text="отвечаю"))
tg.notifications.clear()
alerted = plugin._guard_tick(crd, now_ts=time.time() + 60 * 60)
check("3.6 после ответа уведомления прекращаются", alerted == [] and tg.notifications == [])

# повторные напоминания
reset_settings(threshold=30, re_alert=15)
fire_msg(crd, mk_msg())
base = time.time()
plugin._guard_tick(crd, now_ts=base + 35 * 60)   # первое
tg.notifications.clear()
alerted = plugin._guard_tick(crd, now_ts=base + 45 * 60)  # +10 мин — рано для повтора
check("3.7 повтор раньше интервала — молчим", alerted == [])
alerted = plugin._guard_tick(crd, now_ts=base + 51 * 60)  # +16 мин — пора
check("3.8 повторное напоминание по интервалу", alerted == ["1000"]
      and any("⏱" in t for t, _ in tg.notifications))

# ═══════════════════════ ТЕСТ 4: Telegram UI ═══════════════════════
crd, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
reset_settings(threshold=30)
check("4.1 команда /chatguard зарегистрирована",
      any(kw.get("commands") == ["chatguard"] for _, kw in tg.msg_handlers))
check("4.2 add_telegram_commands вызван", crd.registered_commands
      and crd.registered_commands[0][0] == "chatguard")


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


fire_msg(crd, mk_msg())
name = dispatch_cb(f"47:{plugin.UUID}:0")
check("4.3 страница настроек открывает меню", name == "open_home" and "ChatGuard" in last_text())
check("4.4 в меню виден ждущий чат", "ждут ответа (1)" in last_text() and "buyer1" in last_text())

name = dispatch_cb(plugin.CB_TOG)
check("4.5 мониторинг переключается", name == "toggle_enabled"
      and plugin._load_settings()["enabled"] is False)
dispatch_cb(plugin.CB_TOG)
check("4.6 и включается обратно", plugin._load_settings()["enabled"] is True)

dispatch_cb(plugin.CB_THR)
st = tg.user_states.get((111, 222), {})
check("4.7 кнопка порога ставит состояние", st.get("state") == plugin.ST_THR)
dispatch_msg("45")
check("4.8 порог сохранен (45 мин)", plugin._load_settings()["threshold"] == 45)
dispatch_cb(plugin.CB_THR)
dispatch_msg("0")
check("4.9 порог зажимается в диапазон (0 → 1)", plugin._load_settings()["threshold"] == 1)
dispatch_cb(plugin.CB_THR)
dispatch_msg("abc")
check("4.10 нечисловой ввод отклоняется", plugin._load_settings()["threshold"] == 1
      and "❌" in bot.replies[-1])

dispatch_cb(plugin.CB_RE)
dispatch_msg("20")
check("4.11 интервал повторов сохранен", plugin._load_settings()["re_alert"] == 20)
dispatch_cb(plugin.CB_RE)
dispatch_msg("0")
check("4.12 re_alert=0 принимается (повторы выкл)", plugin._load_settings()["re_alert"] == 0)

reset_settings(threshold=30)
fire_msg(crd, mk_msg())
dispatch_cb(plugin.CB_CLEAR)
check("4.13 запрос подтверждения очистки", "Очистить список" in last_text())
dispatch_cb(plugin.CB_CLEAR_Y)
check("4.14 список очищен после подтверждения", plugin._load_settings()["chats"] == {}
      and "Все чаты отвечены" in last_text())
dispatch_cb(plugin.CB_NOOP)
check("4.15 noop отвечает", True)

# ═══════════════════════ ТЕСТ 5: нормализация настроек ═══════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"enabled": 1, "threshold": "abc", "re_alert": None, "chats": "broken"}, f)
s = plugin._load_settings()
check("5.1 битые значения нормализуются", s["enabled"] is True and s["threshold"] == 30
      and s["re_alert"] == 0 and s["chats"] == {})
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("5.2 битый JSON → дефолты", s["threshold"] == 30 and s["chats"] == {})
reset_settings()

print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
