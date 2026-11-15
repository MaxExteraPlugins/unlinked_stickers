# -*- coding: utf-8 -*-
import os
import time
import traceback
import threading
from typing import Any, Optional

from java import jclass
from java.util import Locale

from org.telegram.messenger import ApplicationLoader, FileLoader, MessageObject, UserConfig
from org.telegram.tgnet import TLRPC

from base_plugin import BasePlugin, MethodHook
from hook_utils import find_class, get_private_field
from ui.settings import Switch, Header, Divider
from ui.bulletin import BulletinHelper


__id__          = "unlinked_stickers"
__name__        = "Unlinked Stickers"
__description__ = "Send stickers from any pack as your own uploaded documents, detached from the original sticker set. \n\nIdea's author: @mishabotov"
__author__      = "@itNotMax - @MaxExteraPlugins"
__version__ = "1.4.0"
__icon__        = "MaxExteraPluginsStickers/26"
__min_version__ = "12.10.6"
__app_version__ = ">=12.10.6"

ENTRY_CLASS = "com.exteraplugins.unlinked_stickers.Main"
DEX_BEGIN   = "__DEX_BEGIN__"
DEX_END     = "__DEX_END__"
PREFS_NAME  = "unlinked_stickers_prefs"

_TGS_MAX_SIZE = 65536


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------

def _get_lang():
    try:
        lang = Locale.getDefault().getLanguage()
        return lang if lang in ("ru", "en") else "en"
    except Exception:
        return "en"


def _t(key):
    lang = _get_lang()
    texts = {
        "settings_header":      {"ru": "Настройки",                                                                           "en": "Settings"},
        "mode_title":           {"ru": "Режим: с паком по умолчанию",                                                         "en": "Mode: send with pack by default"},
        "mode_desc":            {"ru": "При включении: обычная отправка сохраняет пак, кнопка — для отправки без пака",       "en": "When on: normal send keeps pack, button — to send without pack"},
        "preview_btn_title":    {"ru": "Кнопка в превью стикера",                                                             "en": "Button in sticker preview"},
        "preview_btn_desc":     {"ru": "Добавляет кнопку отправки без/со стикерпаком в меню превью",                         "en": "Adds send without/with pack button to preview menu"},
        "block_recents_title":  {"ru": "Не добавлять в недавние",                                                             "en": "Don't add to recents"},
        "block_recents_desc":   {"ru": "Анонимные стикеры не попадают в «Недавние»",                                         "en": "Unlinked stickers won't appear in Recents"},
        "downloading":          {"ru": "Стикер скачивается, нажми ещё раз после загрузки",                                   "en": "Sticker downloading, tap again after download"},
        "anonymize_failed":     {"ru": "Не удалось обезличить стикер. Отправка отменена.",                                   "en": "Failed to anonymize sticker. Send cancelled."},
    }
    return texts.get(key, {}).get(lang, texts.get(key, {}).get("en", key))


# ---------------------------------------------------------------------------
# DEX loader (InMemoryDexClassLoader pattern)
# ---------------------------------------------------------------------------

class Dex:
    def __init__(self, ec):
        self.ec      = ec
        self.clazz   = None
        self.loaded  = False
        self.loading = False
        self._m      = {}

    def load(self):
        if self.loaded or self.loading:
            return
        self.loading = True
        try:
            if self.clazz is None:
                ctx = ApplicationLoader.applicationContext
                if not ctx:
                    raise RuntimeError("no applicationContext")
                raw = self._rp()
                loader_cls = jclass("dalvik.system.InMemoryDexClassLoader")
                loader     = loader_cls(jclass("java.nio.ByteBuffer").wrap(raw), ctx.getClassLoader())
                clazz      = loader.loadClass(self.ec)
                self.clazz = clazz
                for m in self.clazz.getDeclaredMethods():
                    m.setAccessible(True)
                    self._m[str(m.getName())] = m
            self._m["load"].invoke(None)
            self.loaded  = True
            self.loading = False
        except Exception:
            self.loading = False

    def unload(self):
        try:
            if self.loaded:
                self._m["unload"].invoke(None)
        except Exception:
            pass
        self.loaded = False

    def call(self, name, *args):
        try:
            if self.loaded and name in self._m:
                return self._m[name].invoke(None, *args)
        except Exception:
            pass
        return None

    def _rp(self):
        with open(__file__, "r", encoding="utf-8") as f:
            t = f.read()
        if "# " + DEX_BEGIN not in t or "# " + DEX_END not in t:
            raise RuntimeError("DEX markers missing")
        b = t.split("# " + DEX_BEGIN, 1)[1].split("# " + DEX_END, 1)[0]
        p = "".join(l.strip()[1:].strip() for l in b.splitlines() if l.strip().startswith("#"))
        if not p:
            raise RuntimeError("DEX payload empty")
        import zlib, base64
        return zlib.decompress(base64.b64decode(p))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compress_tgs(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if raw[:2] == b"\x1f\x8b":
            return None  # already gzipped
        import gzip
        comp = gzip.compress(raw, compresslevel=9)
        if len(comp) >= len(raw) or len(comp) > _TGS_MAX_SIZE:
            return None
        with open(path, "wb") as fh:
            fh.write(comp)
        return len(comp)
    except Exception:
        return None


def _ensure_sticker_mime_and_name(document, local_file):
    mime  = str(getattr(document, "mime_type", "") or "")
    fname = str(local_file.getName()).lower()
    if mime == "application/x-tgsticker" or fname.endswith(".tgs"):
        document.mime_type = "application/x-tgsticker"
        try:
            attrs = document.attributes
            if attrs is not None:
                for i in range(attrs.size()):
                    attr = attrs.get(i)
                    if isinstance(attr, TLRPC.TL_documentAttributeFilename):
                        attr.file_name = "sticker.tgs"
                        break
        except Exception:
            pass
    elif mime == "video/webm" or fname.endswith(".webm"):
        document.mime_type = "video/webm"
        try:
            attrs = document.attributes
            if attrs is not None:
                for i in range(attrs.size()):
                    attr = attrs.get(i)
                    if isinstance(attr, TLRPC.TL_documentAttributeFilename):
                        attr.file_name = "sticker.webm"
                        break
        except Exception:
            pass


# ---------------------------------------------------------------------------
# sendMessage hook
# ---------------------------------------------------------------------------

class _StickerSendHook:
    def __init__(self, plugin):
        self.plugin = plugin

    def before_hooked_method(self, param: Any) -> None:
        try:
            if not self.plugin.enabled:
                return
            if not param.args or len(param.args) == 0:
                return
            send_params = param.args[0]
            if send_params is None or not hasattr(send_params, "document"):
                return
            result = self.plugin._prepare_sticker(send_params, param.thisObject)
            if result == "cancel":
                param.setResult(None)
        except Exception as e:
            try:
                self.plugin.log("[UnlinkedStickers] send hook error: " + str(e))
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


# ============================================================
# MaxExteraPlugins Shared Author & Channel Subscription Module
# ============================================================
class AuthorChannelHelper:
    CONFIG_URL = "https://raw.githubusercontent.com/MaxExteraPlugins/config/main/links.json"

    DEFAULT_LINKS = {
        "channel_username": "MaxExteraPlugins",
        "channel_url": "https://t.me/MaxExteraPlugins",
        "trial_days": 7,
        "header": {"ru": "MaxExteraPlugins", "en": "MaxExteraPlugins"},
        "buttons": [
            {"title": {"ru": "Канал автора", "en": "Author's Channel"}, "icon": "msg_channel", "url": "https://t.me/MaxExteraPlugins"},
            {"title": {"ru": "Поддержать автора", "en": "Support author"}, "icon": "msg_share", "url": "https://t.me/itNotMax"},
            {"title": {"ru": "Сообщить о баге", "en": "Report a bug"}, "icon": "msg_help", "url": "https://t.me/itNotMax"}
        ]
    }

    _TEXTS = {
        "install_card_title": {"ru": "Спасибо за установку!", "en": "Thank you for installing!"},
        "install_card_msg": {
            "ru": "Надеемся, что плагин будет вам полезен!\n\nМы делаем плагины бесплатными и доступными для всех: пожалуйста, поддержите нас подпиской. Там будут только важные новости, обновления и розыгрыши.",
            "en": "We hope this plugin will be useful to you!\n\nWe keep our plugins free and open for everyone: please support us with a subscription. We only post key news, updates, and giveaways."
        },
        "subscribe_btn": {"ru": "Подписаться", "en": "Subscribe"},
        "remind_later_btn": {"ru": "Напомнить позже", "en": "Remind later"},
        "check_sub_btn": {"ru": "Проверить подписку", "en": "Check subscription"},
        "trial_expired_title": {"ru": "Пожалуйста, подпишитесь на канал", "en": "Please subscribe to the channel"},
        "trial_expired_msg": {
            "ru": "Пожалуйста, подпишитесь на наш официальный канал.\n\nМы делаем плагины бесплатно, ничего не требуя взамен. До подписки функции плагина временно приостановлены.",
            "en": "Please subscribe to our official channel.\n\nWe develop plugins for free and ask nothing in return. Plugin features are paused until you subscribe."
        },
        "sub_thank_you": {"ru": "Спасибо за подписку на наш канал!", "en": "Thank you for subscribing to our channel!"},
        "sub_thank_you_after_expired": {"ru": "Спасибо за подписку на наш канал!\nПлагин продолжит работу!", "en": "Thank you for subscribing to our channel!\nThe plugin will continue working!"},
        "sub_not_found": {"ru": "Подписка не обнаружена. Пожалуйста, подпишитесь на канал.", "en": "Subscription not found. Please subscribe to the channel."},
        "settings_channel": {"ru": "Канал автора", "en": "Author's Channel"},
        "settings_donate": {"ru": "Поддержать автора", "en": "Support author"},
        "settings_bug_report": {"ru": "Сообщить о баге", "en": "Report a bug"}
    }

    @classmethod
    def is_ru(cls) -> bool:
        try:
            from org.telegram.messenger import LocaleController
            cur = str(LocaleController.getInstance().getCurrentLocale().getLanguage() or "").lower()
            if cur.startswith(("ru", "uk", "be")): return True
            if cur: return False
        except Exception:
            pass
        try:
            from java.util import Locale
            code = str(Locale.getDefault().getLanguage() or "").lower()
            if code.startswith(("ru", "uk", "be")): return True
        except Exception:
            pass
        return False

    @classmethod
    def tr(cls, key: str) -> str:
        entry = cls._TEXTS.get(key, {})
        return entry.get("ru" if cls.is_ru() else "en", entry.get("en", key))

    def __init__(self, plugin, icon_sticker=None):
        self.plugin = plugin
        self.icon_sticker = icon_sticker or getattr(plugin, "__icon__", "MaxExteraPluginsStickers/1")
        self._stopped = False
        self._active_dialog = None
        self._last_known_sub = None

    def on_load(self):
        self._stopped = False
        self._active_dialog = None
        self._last_known_sub = None
        self._load_remote_links_async()
        self._check_initial_subscription_flow()
        self._start_subscription_watcher()

    def on_event(self, event_type):
        try:
            name = str(event_type).upper()
            if name.endswith("START") or name.endswith("RESUME"):
                self._check_subscription_change_instant()
        except Exception:
            pass

    def on_unload(self):
        self._stopped = True
        self._dismiss_active_dialog()

    def _get_links(self) -> dict:
        raw = self.plugin.get_setting("remote_links_cache", None)
        if raw:
            try:
                import json
                data = json.loads(raw)
                if isinstance(data, dict): return data
            except Exception:
                pass
        return self.DEFAULT_LINKS

    def _load_remote_links_async(self):
        def worker():
            try:
                import urllib.request, json
                req = urllib.request.Request(self.CONFIG_URL, headers={"User-Agent": "MaxExteraPlugins"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        if isinstance(data, dict):
                            self.plugin.set_setting("remote_links_cache", json.dumps(data))
                            self._resolve_channel_in_telegram(data.get("channel_username", "MaxExteraPlugins"))
            except Exception:
                pass
        import threading
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _open_url(self, url: str, view=None):
        try:
            from org.telegram.messenger.browser import Browser
            from org.telegram.messenger import ApplicationLoader
            from client_utils import get_last_fragment
            ctx = None
            if view is not None:
                ctx = view.getContext()
            if ctx is None:
                frag = get_last_fragment()
                if frag is not None:
                    ctx = frag.getParentActivity()
            if ctx is None:
                ctx = ApplicationLoader.applicationContext
            Browser.openUrl(ctx, url)
        except Exception:
            pass

    def _resolve_channel_in_telegram(self, username: str):
        try:
            from org.telegram.messenger import UserConfig, MessagesController
            from org.telegram.tgnet import TLRPC, ConnectionsManager, RequestDelegate
            clean_user = username.lstrip("@")
            acc = int(UserConfig.selectedAccount)
            mc = MessagesController.getInstance(acc)
            if not mc: return
            req = TLRPC.TL_contacts_resolveUsername()
            req.username = clean_user
            helper_ref = self
            class Delegate(RequestDelegate):
                def run(self, response, error):
                    if error is None and response is not None:
                        chats = getattr(response, "chats", None)
                        if chats is not None and chats.size() > 0:
                            mc.putChats(chats, False)
                            c = chats.get(0)
                            cid = getattr(c, "id", 0)
                            if cid > 0:
                                helper_ref.plugin.set_setting("resolved_channel_id", int(cid))
            ConnectionsManager.getInstance(acc).sendRequest(req, Delegate())
        except Exception:
            pass

    def is_subscribed(self) -> bool:
        try:
            from org.telegram.messenger import UserConfig, MessagesController
            links = self._get_links()
            target = str(links.get("channel_username", "MaxExteraPlugins") or "MaxExteraPlugins").lower().lstrip("@")
            saved_cid = self.plugin.get_setting("resolved_channel_id", 0)

            for acc in range(UserConfig.MAX_ACCOUNT_COUNT):
                uconf = UserConfig.getInstance(acc)
                if not uconf.isClientActivated(): continue
                mc = MessagesController.getInstance(acc)
                if not mc: continue

                if saved_cid:
                    try:
                        from java.lang import Long as JLong
                        c = mc.getChat(JLong(int(saved_cid)))
                        if c is not None:
                            left = bool(getattr(c, "left", True))
                            kicked = bool(getattr(c, "kicked", False))
                            if not left and not kicked: return True
                    except Exception:
                        pass

                try:
                    chat = mc.getUserOrChat(target)
                    if chat is not None:
                        cid = getattr(chat, "id", 0)
                        if cid and not saved_cid:
                            self.plugin.set_setting("resolved_channel_id", int(cid))
                        left = bool(getattr(chat, "left", True))
                        kicked = bool(getattr(chat, "kicked", False))
                        if not left and not kicked: return True
                except Exception:
                    pass

                try:
                    dialogs = mc.allDialogs
                    if dialogs is not None:
                        limit = min(dialogs.size(), 300)
                        for i in range(limit):
                            d = dialogs.get(i)
                            did = getattr(d, "id", 0)
                            if did < 0:
                                from java.lang import Long as JLong
                                c = mc.getChat(JLong(-did))
                                if c is not None:
                                    uname = str(getattr(c, "username", "") or "").lower()
                                    if uname == target:
                                        cid = getattr(c, "id", 0)
                                        if cid and not saved_cid:
                                            self.plugin.set_setting("resolved_channel_id", int(cid))
                                        left = bool(getattr(c, "left", True))
                                        kicked = bool(getattr(c, "kicked", False))
                                        if not left and not kicked: return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def is_allowed(self) -> bool:
        import time
        if self.is_subscribed():
            if not self.plugin.get_setting("was_subscribed", False):
                was_exp = bool(self.plugin.get_setting("was_expired_state", False))
                self.plugin.set_setting("was_subscribed", True)
                self.plugin.set_setting("was_expired_state", False)
                self._notify_subscribed(was_expired=was_exp)
            return True

        if self.plugin.get_setting("was_expired_state", False) or self.plugin.get_setting("was_subscribed", False):
            self.plugin.set_setting("was_subscribed", False)
            self.plugin.set_setting("was_expired_state", True)
            self._show_expired_card()
            return False

        now = time.time()
        install_time = float(self.plugin.get_setting("install_time", 0.0) or 0.0)
        if install_time <= 0:
            install_time = now
            self.plugin.set_setting("install_time", install_time)

        trial_days = float(self._get_links().get("trial_days", 7))
        trial_sec = trial_days * 86400.0

        if (now - install_time) < trial_sec:
            return True

        self.plugin.set_setting("was_expired_state", True)
        self._show_expired_card()
        return False

    def prepend_settings(self, original_items=None) -> list:
        from ui.settings import Header, Text, Divider
        links = self._get_links()
        buttons_config = links.get("buttons")

        author_items = []
        if isinstance(buttons_config, list):
            for btn in buttons_config:
                if not isinstance(btn, dict): continue
                url = str(btn.get("url") or "").strip()
                if not url: continue

                title_data = btn.get("title")
                if isinstance(title_data, dict):
                    title = title_data.get("ru" if self.is_ru() else "en") or title_data.get("en") or title_data.get("ru") or ""
                elif isinstance(title_data, str) and title_data:
                    title = title_data
                else:
                    title = btn.get("title_ru" if self.is_ru() else "title_en") or btn.get("title_ru") or btn.get("title_en") or ""

                if not title: continue
                icon = str(btn.get("icon") or "msg_channel").strip()

                def make_click(u):
                    return lambda v=None: self._open_url(u, v)

                author_items.append(Text(text=title, icon=icon, on_click=make_click(url)))
        else:
            for k, text_key, icon_name in [
                ("channel_url", "settings_channel", "msg_channel"),
                ("donate_url", "settings_donate", "msg_share"),
                ("bug_report_url", "settings_bug_report", "msg_help")
            ]:
                u = str(links.get(k) or "").strip()
                if u:
                    author_items.append(Text(text=self.tr(text_key), icon=icon_name, on_click=lambda v=None, target_u=u: self._open_url(target_u, v)))

        res = []
        if author_items:
            hdr = links.get("header") or "MaxExteraPlugins"
            if isinstance(hdr, dict):
                hdr_text = hdr.get("ru" if self.is_ru() else "en") or hdr.get("ru") or hdr.get("en") or "MaxExteraPlugins"
            else:
                hdr_text = str(hdr)
            res.append(Header(text=hdr_text))
            res.extend(author_items)
            res.append(Divider())

        if original_items:
            res.extend(original_items)
        return res

    def _dismiss_active_dialog(self):
        try:
            if self._active_dialog is not None:
                self._active_dialog.dismiss()
        except Exception:
            pass
        self._active_dialog = None

    def _is_dialog_active(self) -> bool:
        try:
            if self._active_dialog is not None:
                return bool(self._active_dialog.isShowing())
        except Exception:
            pass
        return False

    def _notify_subscribed(self, was_expired=False):
        self.plugin.set_setting("was_subscribed", True)
        from ui.bulletin import BulletinHelper
        msg_key = "sub_thank_you_after_expired" if was_expired else "sub_thank_you"
        BulletinHelper.show_success(self.tr(msg_key))

    def _check_subscription_change_instant(self):
        try:
            from android_utils import run_on_ui_thread
            cur_sub = self.is_subscribed()
            prev_sub = self._last_known_sub
            if prev_sub is not None and prev_sub != cur_sub:
                self._last_known_sub = cur_sub
                if cur_sub:
                    was_exp = bool(self.plugin.get_setting("was_expired_state", False))
                    self.plugin.set_setting("was_subscribed", True)
                    self.plugin.set_setting("was_expired_state", False)
                    self._dismiss_active_dialog()
                    self._notify_subscribed(was_expired=was_exp)
                else:
                    self.plugin.set_setting("was_subscribed", False)
                    self.plugin.set_setting("was_expired_state", True)
                    self._dismiss_active_dialog()
                    self._show_expired_card()
            else:
                self._last_known_sub = cur_sub
        except Exception:
            pass

    def _start_subscription_watcher(self):
        def watcher_loop():
            import time
            from android_utils import run_on_ui_thread
            time.sleep(2.0)
            if self._stopped: return
            self._last_known_sub = self.is_subscribed()

            while not self._stopped:
                try:
                    time.sleep(2.0)
                    if self._stopped: break
                    cur_sub = self.is_subscribed()
                    prev_sub = self._last_known_sub

                    if prev_sub is not None and prev_sub != cur_sub:
                        self._last_known_sub = cur_sub
                        if cur_sub:
                            was_exp = bool(self.plugin.get_setting("was_expired_state", False))
                            self.plugin.set_setting("was_subscribed", True)
                            self.plugin.set_setting("was_expired_state", False)
                            def on_sub():
                                self._dismiss_active_dialog()
                                self._notify_subscribed(was_expired=was_exp)
                            run_on_ui_thread(on_sub)
                        else:
                            self.plugin.set_setting("was_subscribed", False)
                            self.plugin.set_setting("was_expired_state", True)
                            def on_unsub():
                                self._dismiss_active_dialog()
                                self._show_expired_card()
                            run_on_ui_thread(on_unsub)
                    else:
                        self._last_known_sub = cur_sub
                except Exception:
                    pass
        import threading
        t = threading.Thread(target=watcher_loop, daemon=True)
        t.start()

    def _check_initial_subscription_flow(self):
        def flow():
            import time
            try:
                now = time.time()
                install_time = float(self.plugin.get_setting("install_time", 0.0) or 0.0)
                if install_time <= 0:
                    install_time = now
                    self.plugin.set_setting("install_time", install_time)

                is_sub = self.is_subscribed()
                self._last_known_sub = is_sub
                was_sub = bool(self.plugin.get_setting("was_subscribed", False))
                was_exp = bool(self.plugin.get_setting("was_expired_state", False))

                if is_sub:
                    if not was_sub:
                        self.plugin.set_setting("was_subscribed", True)
                        self.plugin.set_setting("was_expired_state", False)
                        self._notify_subscribed(was_expired=was_exp)
                    return

                if was_sub or was_exp:
                    self.plugin.set_setting("was_subscribed", False)
                    self.plugin.set_setting("was_expired_state", True)
                    self._show_expired_card()
                    return

                trial_days = float(self._get_links().get("trial_days", 7))
                trial_sec = trial_days * 86400.0
                last_remind = float(self.plugin.get_setting("last_remind_time", 0.0) or 0.0)

                if (now - install_time) < trial_sec:
                    if last_remind <= 0 or (now - last_remind) >= trial_sec:
                        self._show_welcome_card()
                else:
                    self.plugin.set_setting("was_expired_state", True)
                    self._show_expired_card()
            except Exception:
                pass
        from android_utils import run_on_ui_thread
        run_on_ui_thread(flow, 1500)

    def _create_card_content(self, act, title_text, message_text):
        try:
            from android.widget import LinearLayout, TextView
            from android.view import Gravity
            from android.graphics import Typeface
            from org.telegram.messenger import AndroidUtilities, UserConfig, MediaDataController
            from org.telegram.ui.ActionBar import Theme
            from org.telegram.ui.Components import BackupImageView

            dp = AndroidUtilities.dp
            layout = LinearLayout(act)
            layout.setOrientation(LinearLayout.VERTICAL)
            layout.setGravity(Gravity.CENTER_HORIZONTAL)
            layout.setPadding(dp(24), dp(16), dp(24), dp(6))

            size = dp(76)
            iv = BackupImageView(act)
            iv.setRoundRadius(dp(18))
            lp_iv = LinearLayout.LayoutParams(size, size)
            lp_iv.gravity = Gravity.CENTER_HORIZONTAL
            lp_iv.setMargins(0, dp(4), 0, dp(16))
            iv.setLayoutParams(lp_iv)

            try:
                acc = int(UserConfig.selectedAccount)
                mdc = MediaDataController.getInstance(acc)
                raw_icon = str(self.icon_sticker)
                if "/" in raw_icon:
                    pack, idx_s = raw_icon.split("/", 1)
                    mdc.setPlaceholderImageByIndex(iv, pack, int(idx_s), "150_150")
            except Exception:
                pass
            layout.addView(iv)

            if title_text:
                tt = TextView(act)
                tt.setText(title_text)
                tt.setTextSize(19.0)
                tt.setTypeface(None, Typeface.BOLD)
                tt.setTextColor(Theme.getColor(Theme.key_dialogTextBlack))
                tt.setGravity(Gravity.CENTER_HORIZONTAL)
                lp_t = LinearLayout.LayoutParams(-1, -2)
                lp_t.setMargins(0, 0, 0, dp(12))
                tt.setLayoutParams(lp_t)
                layout.addView(tt)

            if message_text:
                mt = TextView(act)
                mt.setText(message_text)
                mt.setTextSize(15.0)
                mt.setTextColor(Theme.getColor(Theme.key_dialogTextBlack))
                mt.setLineSpacing(float(dp(3)), 1.0)
                mt.setGravity(Gravity.CENTER_HORIZONTAL)
                lp_m = LinearLayout.LayoutParams(-1, -2)
                mt.setLayoutParams(lp_m)
                layout.addView(mt)

            return layout
        except Exception:
            return None

    def _show_welcome_card(self):
        def show():
            try:
                if self._is_dialog_active(): return
                from client_utils import get_last_fragment
                frag = get_last_fragment()
                if not frag: return
                act = frag.getParentActivity()
                if not act: return

                links = self._get_links()
                url = links.get("channel_url", "https://t.me/MaxExteraPlugins")

                from ui.alert import AlertDialogBuilder
                b = AlertDialogBuilder(act)
                content = self._create_card_content(act, self.tr("install_card_title"), self.tr("install_card_msg"))
                if content is not None:
                    b.set_view(content)
                else:
                    b.set_title(self.tr("install_card_title"))
                    b.set_message(self.tr("install_card_msg"))

                def on_sub(d, w):
                    self._dismiss_active_dialog()
                    self._open_url(url)
                    def check_after():
                        if self.is_subscribed():
                            self._notify_subscribed(was_expired=False)
                    from android_utils import run_on_ui_thread
                    run_on_ui_thread(check_after, 3000)

                def on_later(d, w):
                    import time
                    self._dismiss_active_dialog()
                    self.plugin.set_setting("last_remind_time", time.time())

                b.set_positive_button(self.tr("subscribe_btn"), on_sub)
                b.set_negative_button(self.tr("remind_later_btn"), on_later)
                self._active_dialog = b.show()
            except Exception:
                pass
        from android_utils import run_on_ui_thread
        run_on_ui_thread(show)

    def _show_expired_card(self):
        def show():
            try:
                if self._is_dialog_active(): return
                from client_utils import get_last_fragment
                frag = get_last_fragment()
                if not frag: return
                act = frag.getParentActivity()
                if not act: return

                links = self._get_links()
                url = links.get("channel_url", "https://t.me/MaxExteraPlugins")

                from ui.alert import AlertDialogBuilder
                from ui.bulletin import BulletinHelper
                b = AlertDialogBuilder(act)
                content = self._create_card_content(act, self.tr("trial_expired_title"), self.tr("trial_expired_msg"))
                if content is not None:
                    b.set_view(content)
                else:
                    b.set_title(self.tr("trial_expired_title"))
                    b.set_message(self.tr("trial_expired_msg"))

                def on_sub(d, w):
                    self._dismiss_active_dialog()
                    self._open_url(url)
                    def check_after():
                        if self.is_subscribed():
                            self.plugin.set_setting("was_subscribed", True)
                            self.plugin.set_setting("was_expired_state", False)
                            self._notify_subscribed(was_expired=True)
                    from android_utils import run_on_ui_thread
                    run_on_ui_thread(check_after, 3000)

                def on_check(d, w):
                    self._dismiss_active_dialog()
                    if self.is_subscribed():
                        self.plugin.set_setting("was_subscribed", True)
                        self.plugin.set_setting("was_expired_state", False)
                        self._notify_subscribed(was_expired=True)
                    else:
                        BulletinHelper.show_error(self.tr("sub_not_found"))
                        from android_utils import run_on_ui_thread
                        run_on_ui_thread(self._show_expired_card, 500)

                b.set_positive_button(self.tr("subscribe_btn"), on_sub)
                b.set_negative_button(self.tr("check_sub_btn"), on_check)
                self._active_dialog = b.show()
            except Exception:
                pass
        from android_utils import run_on_ui_thread
        run_on_ui_thread(show)


class UnlinkedStickersPlugin(BasePlugin):
    def on_app_event(self, event_type):
        if hasattr(self, "_author_helper"):
            self._author_helper.on_event(event_type)

    def __init__(self):
        super().__init__()
        self._hook_refs              = []
        self._download_request_times = {}
        self._notify_times           = {}
        self.dex                     = Dex(ENTRY_CLASS)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_plugin_load(self):
        self._author_helper = AuthorChannelHelper(self, icon_sticker=globals().get("__icon__", "MaxExteraPluginsStickers/1"))
        self._author_helper.on_load()
        self._push_prefs()
        threading.Thread(target=self._load_dex, daemon=True, name="UnlinkedStickers-init").start()
        self._hook_send_message()

    def on_plugin_unload(self):
        if hasattr(self, "_author_helper"):
            self._author_helper.on_unload()
        for ref in list(self._hook_refs):
            try:
                self.unhook_method(ref)
            except Exception:
                pass
        self._hook_refs.clear()
        self._download_request_times.clear()
        self._notify_times.clear()
        try:
            self.dex.unload()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Settings UI
    # ------------------------------------------------------------------

    def create_settings(self):
        return [
            Header(text=_t("settings_header")),
            Switch(key="mode",          text=_t("mode_title"),          subtext=_t("mode_desc"),          default=False, on_change=self._on_setting_change),
            Switch(key="preview_btn",   text=_t("preview_btn_title"),   subtext=_t("preview_btn_desc"),   default=True,  on_change=self._on_setting_change),
            Switch(key="block_recents", text=_t("block_recents_title"), subtext=_t("block_recents_desc"), default=True,  on_change=self._on_setting_change),
            Divider(),
        ]

    def _on_setting_change(self, value):
        self._push_prefs()

    # ------------------------------------------------------------------
    # Prefs sync  python → java
    # ------------------------------------------------------------------

    def _push_prefs(self):
        try:
            ctx = ApplicationLoader.applicationContext
            if ctx is None:
                return
            sp = ctx.getSharedPreferences(PREFS_NAME, 0)
            ed = sp.edit()
            ed.putBoolean("enabled",       bool(self.enabled))
            ed.putInt("mode",              1 if bool(self.get_setting("mode", False)) else 0)
            ed.putBoolean("preview_btn",   bool(self.get_setting("preview_btn",   True)))
            ed.putBoolean("block_recents", bool(self.get_setting("block_recents", True)))
            ed.apply()
            if self.dex.loaded:
                self.dex.call("pyFlushConfig")
        except Exception as e:
            self.log("[UnlinkedStickers] push_prefs error: " + str(e))

    # ------------------------------------------------------------------
    # DEX init
    # ------------------------------------------------------------------

    def _load_dex(self):
        try:
            self.dex.load()
            self._push_prefs()
        except Exception as e:
            self.log("[UnlinkedStickers] dex load error: " + str(e))

    # ------------------------------------------------------------------
    # sendMessage hook registration
    # ------------------------------------------------------------------

    def _hook_send_message(self):
        cls = find_class("org.telegram.messenger.SendMessagesHelper")
        if cls is None:
            return
        try:
            methods = list(cls.getClass().getDeclaredMethods())
        except Exception:
            methods = []
        hooked   = 0
        hook_obj = _StickerSendHook(self)
        for method in methods:
            try:
                if method is None or method.getName() != "sendMessage":
                    continue
                method.setAccessible(True)
                ref = self.hook_method(method, hook_obj)
                if ref is not None:
                    self._hook_refs.append(ref)
                    hooked += 1
            except Exception:
                pass
        self.log("[UnlinkedStickers] hooked sendMessage overloads: " + str(hooked))

    # ------------------------------------------------------------------
    # Core logic: prepare sticker for anonymous send
    # ------------------------------------------------------------------

    def _prepare_sticker(self, send_params: Any, send_helper: Any) -> Optional[str]:
        document = getattr(send_params, "document", None)
        if document is None:
            return None

        if not self._looks_like_sticker(document):
            return None

        sticker_attr = self._get_sticker_attribute(document)
        if sticker_attr is None:
            return None

        doc_id = int(getattr(document, "id", 0) or 0)

        # Ask Java which bypass is pending for this doc_id (atomic consume)
        bypass = 0
        try:
            if self.dex.loaded:
                bypass = int(self.dex.call("pyCheckAndConsumeBypass", doc_id) or 0)
        except Exception:
            pass

        # bypass=1 → user pressed "Send with pack" → keep pack, skip anonymization
        if bypass == 1:
            return None

        mode_reverse = bool(self.get_setting("mode", False))

        # bypass=2 → user pressed "Send without pack" in reverse mode → force anonymize
        # normal mode (reverse=False): always anonymize
        # reverse mode (reverse=True): keep pack by default, anonymize only on bypass=2
        should_anonymize = (bypass == 2) or (not mode_reverse)
        if not should_anonymize:
            return None

        if self._is_already_anonymous_upload(document, send_params):
            self._strip_runtime_pack_hints(send_params)
            return None

        account    = self._resolve_account(send_helper)
        local_file = self._find_existing_sticker_file(account, document)
        if local_file is None:
            self._request_download_if_needed(account, document, getattr(send_params, "parentObject", None))
            self._notify_once(doc_id, _t("downloading"), is_error=False)
            return "cancel"

        try:
            anon_doc = self._clone_document_for_upload(document, local_file)
            setattr(send_params, "document", anon_doc)
            setattr(send_params, "path",     local_file.getAbsolutePath())
            self._strip_runtime_pack_hints(send_params)
            return None
        except Exception:
            self._show_error(_t("anonymize_failed"))
            return "cancel"

    # ------------------------------------------------------------------
    # Sticker classification helpers
    # ------------------------------------------------------------------

    def _looks_like_sticker(self, document: Any) -> bool:
        try:
            return bool(
                MessageObject.isStickerDocument(document)
                or MessageObject.isAnimatedStickerDocument(document, True)
                or MessageObject.isVideoSticker(document)
            )
        except Exception:
            mime = str(getattr(document, "mime_type", "") or "")
            return mime in ("image/webp", "application/x-tgsticker", "video/webm")

    def _get_sticker_attribute(self, document: Any) -> Any:
        attrs = getattr(document, "attributes", None)
        if attrs is None:
            return None
        try:
            for i in range(attrs.size()):
                attr = attrs.get(i)
                if isinstance(attr, TLRPC.TL_documentAttributeSticker):
                    return attr
        except Exception:
            try:
                for attr in attrs:
                    if isinstance(attr, TLRPC.TL_documentAttributeSticker):
                        return attr
            except Exception:
                return None
        return None

    def _is_already_anonymous_upload(self, document: Any, send_params: Any) -> bool:
        try:
            if int(getattr(document, "access_hash", 0) or 0) != 0:
                return False
            path = getattr(send_params, "path", None)
            if not path:
                return False
            attr        = self._get_sticker_attribute(document)
            sticker_set = getattr(attr, "stickerset", None) if attr is not None else None
            return sticker_set is None or isinstance(sticker_set, TLRPC.TL_inputStickerSetEmpty)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # File access helpers
    # ------------------------------------------------------------------

    def _find_existing_sticker_file(self, account: int, document: Any) -> Any:
        file_loader = FileLoader.getInstance(int(account))
        candidates  = []
        for force_cache in (False, True):
            try:
                f = file_loader.getPathToAttach(document, force_cache)
                if f is not None:
                    candidates.append(f)
            except Exception:
                pass
        try:
            local_path = getattr(document, "localPath", None)
            if local_path:
                from java.io import File
                candidates.append(File(str(local_path)))
        except Exception:
            pass
        for f in candidates:
            try:
                if f is not None and f.exists() and f.length() > 0:
                    return f
            except Exception:
                pass
        return None

    def _request_download_if_needed(self, account: int, document: Any, parent_object: Any) -> None:
        try:
            doc_id = int(getattr(document, "id", 0) or 0)
            now    = time.time()
            last   = float(self._download_request_times.get(doc_id, 0) or 0)
            if now - last < 2.0:
                return
            self._download_request_times[doc_id] = now
            # Literal 3 = FileLoader.PRIORITY_HIGH (safe across all host versions)
            FileLoader.getInstance(int(account)).loadFile(document, parent_object, 3, 0)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Document cloning helpers
    # ------------------------------------------------------------------

    def _clone_document_for_upload(self, document: Any, local_file: Any) -> Any:
        new_doc              = TLRPC.TL_document()
        new_doc.flags        = 0
        new_doc.id           = 0
        new_doc.access_hash  = 0
        new_doc.file_reference = bytearray()
        new_doc.date         = int(time.time())
        new_doc.mime_type    = str(getattr(document, "mime_type", "") or "")
        try:
            new_doc.size = local_file.length()
        except Exception:
            new_doc.size = int(getattr(document, "size", 0) or 0)
        new_doc.dc_id = 0
        try:
            new_doc.localPath = local_file.getAbsolutePath()
        except Exception:
            pass

        self._copy_safe_thumbs(document, new_doc)
        self._copy_attributes_without_pack(document, new_doc)

        try:
            fname = local_file.getName().lower()
            if fname.endswith(".tgs") or new_doc.mime_type == "application/x-tgsticker":
                new_doc.mime_type = "application/x-tgsticker"
                compressed = _compress_tgs(local_file.getAbsolutePath())
                if compressed is not None:
                    new_doc.size = compressed
            elif fname.endswith(".webm") or new_doc.mime_type == "video/webm":
                new_doc.mime_type = "video/webm"
        except Exception:
            pass

        _ensure_sticker_mime_and_name(new_doc, local_file)
        return new_doc

    def _copy_safe_thumbs(self, source: Any, target: Any) -> None:
        copied = False
        try:
            thumbs = getattr(source, "thumbs", None)
            if thumbs is not None:
                for i in range(thumbs.size()):
                    thumb = thumbs.get(i)
                    if isinstance(thumb, TLRPC.TL_photoStrippedSize):
                        target.thumbs.add(thumb)
                        copied = True
        except Exception:
            copied = False
        if copied:
            try:
                target.flags = int(getattr(target, "flags", 0) or 0) | 1
            except Exception:
                pass

    def _copy_attributes_without_pack(self, source: Any, target: Any) -> None:
        attrs = getattr(source, "attributes", None)
        if attrs is None:
            return
        has_filename = False
        try:
            count = attrs.size()
            for i in range(count):
                attr = attrs.get(i)
                if isinstance(attr, TLRPC.TL_documentAttributeSticker):
                    target.attributes.add(self._anonymous_sticker_attribute(attr))
                else:
                    if isinstance(attr, TLRPC.TL_documentAttributeFilename):
                        has_filename = True
                    target.attributes.add(attr)
        except Exception:
            for attr in attrs:
                if isinstance(attr, TLRPC.TL_documentAttributeSticker):
                    target.attributes.add(self._anonymous_sticker_attribute(attr))
                else:
                    if isinstance(attr, TLRPC.TL_documentAttributeFilename):
                        has_filename = True
                    target.attributes.add(attr)
        if not has_filename:
            fn_attr           = TLRPC.TL_documentAttributeFilename()
            fn_attr.file_name = self._default_file_name(str(getattr(source, "mime_type", "") or ""))
            target.attributes.add(fn_attr)

    def _anonymous_sticker_attribute(self, source_attr: Any) -> Any:
        attr = TLRPC.TL_documentAttributeSticker()
        try:
            attr.flags = int(getattr(source_attr, "flags", 0) or 0)
        except Exception:
            attr.flags = 0
        try:
            attr.alt = str(getattr(source_attr, "alt", "") or "")
        except Exception:
            attr.alt = ""
        try:
            attr.mask = bool(getattr(source_attr, "mask", False))
        except Exception:
            attr.mask = False
        try:
            attr.mask_coords = getattr(source_attr, "mask_coords", None)
        except Exception:
            pass
        attr.stickerset = TLRPC.TL_inputStickerSetEmpty()
        return attr

    def _default_file_name(self, mime_type: str) -> str:
        if mime_type == "application/x-tgsticker":
            return "sticker.tgs"
        if mime_type == "video/webm":
            return "sticker.webm"
        return "sticker.webp"

    # ------------------------------------------------------------------
    # Send params cleanup
    # ------------------------------------------------------------------

    def _strip_runtime_pack_hints(self, send_params: Any) -> None:
        try:
            setattr(send_params, "updateStickersOrder", False)
        except Exception:
            pass
        try:
            setattr(send_params, "parentObject", None)
        except Exception:
            pass
        try:
            params = getattr(send_params, "params", None)
            if params is not None:
                try:
                    params.remove("query")
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Account resolution
    # ------------------------------------------------------------------

    def _resolve_account(self, send_helper: Any) -> int:
        for field_name in ("currentAccount",):
            try:
                value = get_private_field(send_helper, field_name)
                if value is not None:
                    return int(value)
            except Exception:
                pass
            try:
                value = getattr(send_helper, field_name)
                if value is not None:
                    return int(value)
            except Exception:
                pass
        try:
            return int(UserConfig.selectedAccount)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _notify_once(self, doc_id: int, text: str, is_error: bool = False) -> None:
        now  = time.time()
        last = float(self._notify_times.get(doc_id, 0) or 0)
        if now - last < 1.5:
            return
        self._notify_times[doc_id] = now
        if is_error:
            self._show_error(text)
        else:
            self._show_info(text)

    def _show_info(self, text: str) -> None:
        try:
            BulletinHelper.show_info(text)
        except Exception:
            self.log("[UnlinkedStickers] " + text)

    def _show_error(self, text: str) -> None:
        try:
            BulletinHelper.show_error(text)
        except Exception:
            self.log("[UnlinkedStickers] " + text)

# __DEX_BEGIN__
# PLACEHOLDER
# __DEX_END__
