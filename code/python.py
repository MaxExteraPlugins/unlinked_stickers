# -*- coding: utf-8 -*-
import os
import time
import traceback
import threading
from typing import Any, Optional

from java import jclass
from java.util import Locale

from org.telegram.messenger import ApplicationLoader, FileLoader, MessageObject, SendMessagesHelper, UserConfig
from org.telegram.tgnet import TLRPC

from base_plugin import BasePlugin, MethodHook
from hook_utils import find_class, get_private_field
from ui.settings import Switch, Header, Divider
from ui.bulletin import BulletinHelper


__id__          = "unlinked_stickers"
__name__        = "Unlinked Stickers"
__description__ = "Send stickers from any pack as your own uploaded documents, detached from the original sticker set. \n\nIdea's author: @mishabotov"
__author__      = "@itNotMax - @MaxExteraPlugins"
__version__ = "1.4.4"
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

# ============================================================
# MaxExteraPlugins Shared Author & Channel Subscription Module
# ============================================================

# ============================================================
# MaxExteraPlugins Shared Author & Channel Subscription Module
# ============================================================
class AuthorChannelHelper:
    CONFIG_URL = "https://raw.githubusercontent.com/MaxExteraPlugins/config/main/links.json"

    DEFAULT_LINKS = {
        "channel_username": "MaxExteraPlugins",
        "channel_url": "https://t.me/MaxExteraPlugins",
        "header": {"ru": "MaxExteraPlugins", "en": "MaxExteraPlugins"},
        "buttons": [
            {"title": {"ru": "Канал автора", "en": "Author's Channel"}, "icon": "msg_channel", "url": "https://t.me/MaxExteraPlugins"},
            {"title": {"ru": "Поддержать автора", "en": "Support author"}, "icon": "msg_share", "url": "https://t.me/itNotMax"},
            {"title": {"ru": "Сообщить о баге", "en": "Report a bug"}, "icon": "msg_help", "url": "https://t.me/itNotMax"}
        ]
    }

    _TEXTS = {
        "install_card_title": {"ru": "Подпишитесь на канал", "en": "Subscribe to channel"},
        "install_card_msg": {
            "ru": (
                "Мы делаем плагины бесплатными и доступными для всех: пожалуйста, "
                "поддержите нас подпиской на официальный канал @MaxExteraPlugins.\n\n"
                "Там публикуются обновления, важные новости и анонсы новых плагинов."
            ),
            "en": (
                "We keep our plugins free and accessible for everyone: please "
                "support us by subscribing to our official channel @MaxExteraPlugins.\n\n"
                "Updates, news, and new plugin announcements are published there."
            )
        },
        "open_channel_btn": {"ru": "Перейти на канал", "en": "Open channel"},
        "remind_later_btn": {"ru": "Напомнить позже", "en": "Remind later"},
        "never_show_btn": {"ru": "Больше не показывать", "en": "Don't show again"},
        "sub_thank_you": {"ru": "Спасибо за подписку на наш канал!", "en": "Thank you for subscribing to our channel!"},
        "settings_channel": {"ru": "Канал автора", "en": "Author's Channel"},
        "settings_donate": {"ru": "Поддержать автора", "en": "Support author"},
        "settings_bug_report": {"ru": "Сообщить о баге", "en": "Report a bug"}
    }

    @classmethod
    def is_ru(cls) -> bool:
        try:
            from org.telegram.messenger import LocaleController
            lang = str(LocaleController.getInstance().getCurrentLocale().getLanguage() or "").lower()
            return lang in ("ru", "be", "uk", "kk")
        except Exception:
            return True

    @classmethod
    def tr(cls, key: str) -> str:
        entry = cls._TEXTS.get(key, {})
        return entry.get("ru" if cls.is_ru() else "en", entry.get("en", key))

    def __init__(self, plugin, icon_sticker=None):
        self.plugin = plugin
        self.icon_sticker = icon_sticker or getattr(plugin, "__icon__", "MaxExteraPluginsStickers/1")
        self._stopped = False
        self._active_dialog = None

    def on_load(self):
        self._stopped = False
        self._active_dialog = None
        self._load_remote_links_async()
        self._check_initial_subscription_flow()

    def on_event(self, event_type):
        try:
            name = str(event_type).upper()
            if name.endswith("START") or name.endswith("RESUME"):
                if self.is_subscribed() and not self.plugin.get_setting("was_subscribed", False):
                    self.plugin.set_setting("was_subscribed", True)
                    self._dismiss_active_dialog()
                    self._notify_subscribed()
        except Exception:
            pass

    def on_unload(self):
        self._stopped = True
        self._dismiss_active_dialog()

    def is_allowed(self) -> bool:
        return True

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
                            for a in range(UserConfig.MAX_ACCOUNT_COUNT):
                                try:
                                    if UserConfig.getInstance(a).isClientActivated():
                                        MessagesController.getInstance(a).putChats(chats, False)
                                except Exception:
                                    pass
                            c = chats.get(0)
                            cid = getattr(c, "id", 0)
                            if cid > 0:
                                helper_ref.plugin.set_setting("resolved_channel_id", int(cid))
            ConnectionsManager.getInstance(acc).sendRequest(req, Delegate())
        except Exception:
            pass

    def is_subscribed(self) -> bool:
        try:
            from org.telegram.messenger import UserConfig, MessagesController, MessagesStorage
            from java.lang import Long as JLong
            links = self._get_links()
            target = str(links.get("channel_username", "MaxExteraPlugins") or "MaxExteraPlugins").lower().lstrip("@")
            saved_cid = int(self.plugin.get_setting("resolved_channel_id", 0) or 0)

            for acc in range(UserConfig.MAX_ACCOUNT_COUNT):
                try:
                    uconf = UserConfig.getInstance(acc)
                    if not uconf.isClientActivated():
                        continue

                    mc = MessagesController.getInstance(acc)
                    storage = MessagesStorage.getInstance(acc)

                    # 1. In-memory check by resolved_channel_id
                    if saved_cid and mc is not None:
                        try:
                            c = mc.getChat(JLong(saved_cid))
                            if c is not None:
                                left = bool(getattr(c, "left", True))
                                kicked = bool(getattr(c, "kicked", False))
                                if not left and not kicked:
                                    return True
                        except Exception:
                            pass

                    # 2. In-memory check by username
                    if mc is not None:
                        try:
                            chat = mc.getUserOrChat(target)
                            if chat is not None:
                                cid = int(getattr(chat, "id", 0) or 0)
                                if cid and not saved_cid:
                                    saved_cid = cid
                                    self.plugin.set_setting("resolved_channel_id", cid)
                                left = bool(getattr(chat, "left", True))
                                kicked = bool(getattr(chat, "kicked", False))
                                if not left and not kicked:
                                    return True
                        except Exception:
                            pass

                    # 3. In-memory dialogs check
                    if mc is not None:
                        try:
                            dialog_lists = []
                            d_chan = getattr(mc, "dialogsChannelsOnly", None)
                            if d_chan is not None and d_chan.size() > 0:
                                dialog_lists.append(d_chan)
                            d_all = getattr(mc, "allDialogs", None)
                            if d_all is not None and d_all.size() > 0:
                                dialog_lists.append(d_all)

                            for d_list in dialog_lists:
                                limit = min(d_list.size(), 300)
                                for i in range(limit):
                                    d = d_list.get(i)
                                    did = getattr(d, "id", 0)
                                    if did < 0:
                                        cid = -did
                                        if saved_cid and cid == saved_cid:
                                            c = mc.getChat(JLong(cid))
                                            if c is not None:
                                                left = bool(getattr(c, "left", True))
                                                kicked = bool(getattr(c, "kicked", False))
                                                if not left and not kicked:
                                                    return True
                                        else:
                                            c = mc.getChat(JLong(cid))
                                            if c is not None:
                                                uname = str(getattr(c, "username", "") or "").lower()
                                                if uname == target:
                                                    if not saved_cid:
                                                        saved_cid = cid
                                                        self.plugin.set_setting("resolved_channel_id", cid)
                                                    left = bool(getattr(c, "left", True))
                                                    kicked = bool(getattr(c, "kicked", False))
                                                    if not left and not kicked:
                                                        return True
                        except Exception:
                            pass

                    # 4. Storage check (SQLite on disk)
                    if storage is not None:
                        if saved_cid:
                            try:
                                if storage.containsLocalDialog(JLong(-saved_cid)):
                                    c_sync = storage.getChatSync(JLong(saved_cid))
                                    if c_sync is not None:
                                        left = bool(getattr(c_sync, "left", True))
                                        kicked = bool(getattr(c_sync, "kicked", False))
                                        if not left and not kicked:
                                            return True
                                    else:
                                        return True
                            except Exception:
                                pass

                        try:
                            db = storage.getDatabase()
                            if db is not None:
                                if saved_cid:
                                    try:
                                        cur = db.queryFinalized("SELECT did FROM dialogs WHERE did = ?", [JLong(-saved_cid)])
                                        if cur is not None:
                                            has_row = bool(cur.next())
                                            cur.dispose()
                                            if has_row:
                                                return True
                                    except Exception:
                                        pass

                                try:
                                    cur = db.queryFinalized("SELECT uid, name FROM chats WHERE LOWER(name) LIKE ?", ["%" + target + "%"])
                                    if cur is not None:
                                        while cur.next():
                                            c_id = cur.longValue(0)
                                            if c_id > 0:
                                                if not saved_cid:
                                                    saved_cid = int(c_id)
                                                    self.plugin.set_setting("resolved_channel_id", saved_cid)
                                                cur_d = db.queryFinalized("SELECT did FROM dialogs WHERE did = ?", [JLong(-c_id)])
                                                if cur_d is not None:
                                                    in_dlg = bool(cur_d.next())
                                                    cur_d.dispose()
                                                    if in_dlg:
                                                        cur.dispose()
                                                        return True
                                        cur.dispose()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def prepend_settings(self, original_items=None) -> list:
        from ui.settings import Header, Text, Divider

        if original_items and any(getattr(x, "text", "") == "MaxExteraPlugins" for x in original_items if type(x).__name__ == "Header"):
            return original_items

        links = self._get_links()
        buttons_config = links.get("buttons")

        author_items = []
        if isinstance(buttons_config, list):
            for btn in buttons_config:
                if not isinstance(btn, dict): continue
                raw_url = btn.get("url")
                if isinstance(raw_url, dict):
                    keys = [
                        getattr(self.plugin, "id", None),
                        globals().get("__id__", None),
                        globals().get("__name__", None),
                        self.plugin.__class__.__name__
                    ]
                    keys += [k.lower() for k in keys if isinstance(k, str)]
                    resolved_url = ""
                    for k in keys:
                        if k and k in raw_url:
                            resolved_url = str(raw_url[k] or "").strip()
                            if resolved_url:
                                break
                    url = resolved_url
                elif isinstance(raw_url, str):
                    url = raw_url.strip()
                else:
                    url = ""

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

        cleaned_items = []
        if original_items:
            skipping_info = False
            for item in original_items:
                item_cls = type(item).__name__
                if item_cls == "Header":
                    txt = str(getattr(item, "text", "") or getattr(item, "title", "") or "").lower()
                    if "информ" in txt or "inform" in txt:
                        skipping_info = True
                        continue
                    else:
                        skipping_info = False
                elif skipping_info and item_cls == "Divider":
                    skipping_info = False
                    continue

                if not skipping_info:
                    cleaned_items.append(item)

        res.extend(cleaned_items)
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

    def _notify_subscribed(self):
        try:
            from ui.bulletin import BulletinHelper
            BulletinHelper.show_success(self.tr("sub_thank_you"))
        except Exception:
            pass

    def _check_initial_subscription_flow(self):
        def flow():
            try:
                if self.plugin.get_setting("never_show_sub_prompt", False):
                    return

                current_ver = str(getattr(self.plugin, "__version__", globals().get("__version__", "1.0.0")) or "1.0.0")
                last_prompted_ver = self.plugin.get_setting("last_prompted_version", None)

                # Only show on install (last_prompted_ver is None) or update (last_prompted_ver != current_ver)
                if last_prompted_ver is not None and str(last_prompted_ver) == current_ver:
                    return

                if self.is_subscribed():
                    self.plugin.set_setting("last_prompted_version", current_ver)
                    self.plugin.set_setting("was_subscribed", True)
                    return

                self._show_subscription_prompt(current_ver)
            except Exception:
                pass
        from android_utils import run_on_ui_thread
        run_on_ui_thread(flow, 1500)

    def _create_card_content(self, act, title_text, message_text):
        try:
            from android.widget import LinearLayout, TextView, ImageView
            from android.view import Gravity
            from org.telegram.messenger import AndroidUtilities
            from org.telegram.ui.ActionBar import Theme
            from client_utils import create_sticker_drawable

            dp = AndroidUtilities.dp

            layout = LinearLayout(act)
            layout.setOrientation(LinearLayout.VERTICAL)
            layout.setGravity(Gravity.CENTER_HORIZONTAL)
            layout.setPadding(dp(24), dp(20), dp(24), dp(10))

            sticker_drawable = None
            if self.icon_sticker:
                try:
                    sticker_drawable = create_sticker_drawable(self.icon_sticker, dp(120), dp(120))
                except Exception:
                    pass

            if sticker_drawable is not None:
                iv = ImageView(act)
                iv.setImageDrawable(sticker_drawable)
                lp_iv = LinearLayout.LayoutParams(dp(120), dp(120))
                lp_iv.gravity = Gravity.CENTER_HORIZONTAL
                lp_iv.bottomMargin = dp(14)
                layout.addView(iv, lp_iv)

            if title_text:
                tt = TextView(act)
                tt.setText(title_text)
                tt.setTextSize(19.0)
                tt.setTypeface(AndroidUtilities.bold())
                tt.setTextColor(Theme.getColor(Theme.key_dialogTextBlack))
                tt.setGravity(Gravity.CENTER_HORIZONTAL)
                lp_t = LinearLayout.LayoutParams(-1, -2)
                lp_t.bottomMargin = dp(8)
                layout.addView(tt, lp_t)

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

    def _show_subscription_prompt(self, current_ver: str):
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

                def on_open_channel(dialog, which):
                    self._dismiss_active_dialog()
                    self.plugin.set_setting("last_prompted_version", current_ver)
                    self._open_url(url)

                def on_remind_later(dialog, which):
                    self._dismiss_active_dialog()
                    self.plugin.set_setting("last_prompted_version", current_ver)

                def on_never_show(dialog, which):
                    self._dismiss_active_dialog()
                    self.plugin.set_setting("never_show_sub_prompt", True)
                    self.plugin.set_setting("last_prompted_version", current_ver)

                b.set_positive_button(self.tr("open_channel_btn"), on_open_channel)
                b.set_neutral_button(self.tr("remind_later_btn"), on_remind_later)
                b.set_negative_button(self.tr("never_show_btn"), on_never_show)

                def on_dismiss(builder):
                    self.plugin.set_setting("last_prompted_version", current_ver)
                try:
                    b.set_on_dismiss_listener(on_dismiss)
                except Exception:
                    pass

                self._active_dialog = b.show()
                self.plugin.set_setting("last_prompted_version", current_ver)
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
        self._waiting_downloads      = set()   # doc_ids ожидающих загрузки
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
        self._waiting_downloads.clear()
        try:
            self.dex.unload()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Settings UI
    # ------------------------------------------------------------------

    def create_settings(self):
        try:
            items = self._inner_create_settings()
        except Exception:
            items = []
        if hasattr(self, '_author_helper'):
            return self._author_helper.prepend_settings(items)
        return items

    def _inner_create_settings(self):
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
            # Запускаем загрузку и фоновый поток, который сам отправит после скачивания
            if doc_id not in self._waiting_downloads:
                self._waiting_downloads.add(doc_id)
                parent_object = getattr(send_params, "parentObject", None)
                try:
                    FileLoader.getInstance(int(account)).loadFile(document, parent_object, 3, 0)
                except Exception:
                    pass
                threading.Thread(
                    target=self._wait_and_send,
                    args=(account, document, send_params, send_helper, doc_id),
                    daemon=True,
                    name="UnlinkedStickers-wait",
                ).start()
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
        # Быстрая проверка mime: GIF/видео/аудио/фото не трогаем до вызова Java
        mime = str(getattr(document, "mime_type", "") or "")
        if mime and mime not in ("image/webp", "application/x-tgsticker", "video/webm"):
            return False
        try:
            return bool(
                MessageObject.isStickerDocument(document)
                or MessageObject.isAnimatedStickerDocument(document, True)
                or MessageObject.isVideoSticker(document)
            )
        except Exception:
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
    # Auto-send after download
    # ------------------------------------------------------------------

    def _wait_and_send(self, account: int, document: Any, send_params: Any, send_helper: Any, doc_id: int) -> None:
        """Ждёт появления файла на диске (до 60 с), затем клонирует и отправляет."""
        try:
            deadline = time.time() + 60.0
            while time.time() < deadline:
                time.sleep(0.5)
                if not self.enabled:
                    return
                local_file = self._find_existing_sticker_file(account, document)
                if local_file is not None:
                    try:
                        anon_doc = self._clone_document_for_upload(document, local_file)
                        setattr(send_params, "document", anon_doc)
                        setattr(send_params, "path",     local_file.getAbsolutePath())
                        self._strip_runtime_pack_hints(send_params)
                        # Повторный вызов sendMessage; хук определит документ как уже анонимный
                        # (_is_already_anonymous_upload → True) и пропустит без изменений
                        SendMessagesHelper.getInstance(int(account)).sendMessage(send_params)
                    except Exception as e:
                        self.log("[UnlinkedStickers] wait_and_send error: " + str(e))
                        self._show_error(_t("anonymize_failed"))
                    return
            # Таймаут — стикер так и не скачался
            self.log("[UnlinkedStickers] download timeout for doc_id=" + str(doc_id))
            self._show_error(_t("anonymize_failed"))
        finally:
            self._waiting_downloads.discard(doc_id)

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
