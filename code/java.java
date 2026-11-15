package com.exteraplugins.unlinked_stickers;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;

import org.telegram.messenger.AndroidUtilities;
import org.telegram.messenger.ApplicationLoader;
import org.telegram.messenger.LocaleController;
import org.telegram.messenger.MediaDataController;
import org.telegram.messenger.MessageObject;
import org.telegram.messenger.SendMessagesHelper;
import org.telegram.messenger.UserConfig;
import org.telegram.tgnet.TLRPC;
import org.telegram.ui.ActionBar.ActionBarMenuItem;
import org.telegram.ui.ActionBar.ActionBarMenuSubItem;
import org.telegram.ui.ActionBar.ActionBarPopupWindow;
import org.telegram.ui.ActionBar.BaseFragment;
import org.telegram.ui.ActionBar.Theme;
import org.telegram.ui.ChatActivity;
import org.telegram.ui.ContentPreviewViewer;
import org.telegram.ui.LaunchActivity;

public class Main {
    private static final String TAG = "UnlinkedStickers";
    private static final String PREFS_NAME = "unlinked_stickers_prefs";

    private static volatile boolean loaded = false;
    private static volatile boolean enabled = true;
    private static volatile int mode = 0; // 0 = always unlinked, 1 = default with pack
    private static volatile boolean showPreviewButton = true;
    private static volatile boolean blockRecents = true;

    private static final Set<Long> bypassSendWithPack = Collections.synchronizedSet(new HashSet<Long>());
    private static final Set<Long> bypassSendWithoutPack = Collections.synchronizedSet(new HashSet<Long>());
    private static final List<XC_MethodHook.Unhook> hooks = new ArrayList<XC_MethodHook.Unhook>();

    private static Field fCurrentDocument;
    private static Field fCurrentQuery;
    private static Field fDelegate;
    private static Field fParentObject;
    private static Field fPopupWindow;
    private static Field fPopupLayout;
    private static Field fResourcesProvider;

    public static void load() {
        if (loaded) return;
        loaded = true;
        pyFlushConfig();
        initReflection();
        hookContentPreviewViewer();
        hookMediaDataController();
        Log.d(TAG, "DEX loaded");
    }

    public static void unload() {
        for (XC_MethodHook.Unhook u : hooks) {
            try {
                u.unhook();
            } catch (Throwable ignored) {}
        }
        hooks.clear();
        bypassSendWithPack.clear();
        bypassSendWithoutPack.clear();
        loaded = false;
        Log.d(TAG, "DEX unloaded");
    }

    public static void pyFlushConfig() {
        try {
            Context ctx = ApplicationLoader.applicationContext;
            if (ctx == null) return;
            SharedPreferences sp = ctx.getSharedPreferences(PREFS_NAME, 0);
            enabled = sp.getBoolean("enabled", true);
            mode = sp.getInt("mode", 0);
            showPreviewButton = sp.getBoolean("preview_btn", true);
            blockRecents = sp.getBoolean("block_recents", true);
        } catch (Throwable t) {
            Log.e(TAG, "pyFlushConfig error", t);
        }
    }

    public static int pyCheckAndConsumeBypass(long docId) {
        if (bypassSendWithPack.remove(docId)) return 1;
        if (bypassSendWithoutPack.remove(docId)) return 2;
        return 0;
    }

    private static void initReflection() {
        try {
            fCurrentDocument = ContentPreviewViewer.class.getDeclaredField("currentDocument");
            fCurrentDocument.setAccessible(true);
        } catch (Throwable ignored) {}
        try {
            fCurrentQuery = ContentPreviewViewer.class.getDeclaredField("currentQuery");
            fCurrentQuery.setAccessible(true);
        } catch (Throwable ignored) {}
        try {
            fDelegate = ContentPreviewViewer.class.getDeclaredField("delegate");
            fDelegate.setAccessible(true);
        } catch (Throwable ignored) {}
        try {
            fParentObject = ContentPreviewViewer.class.getDeclaredField("parentObject");
            fParentObject.setAccessible(true);
        } catch (Throwable ignored) {}
        try {
            fPopupWindow = ContentPreviewViewer.class.getDeclaredField("popupWindow");
            fPopupWindow.setAccessible(true);
        } catch (Throwable ignored) {}
        try {
            fPopupLayout = ContentPreviewViewer.class.getDeclaredField("popupLayout");
            fPopupLayout.setAccessible(true);
        } catch (Throwable ignored) {}
        try {
            fResourcesProvider = ContentPreviewViewer.class.getDeclaredField("resourcesProvider");
            fResourcesProvider.setAccessible(true);
        } catch (Throwable ignored) {}
    }

    private static void hookContentPreviewViewer() {
        try {
            for (Method m : ContentPreviewViewer.class.getDeclaredMethods()) {
                if ("showMenuFor".equals(m.getName())) {
                    hooks.add(XposedBridge.hookMethod(m, new XC_MethodHook() {
                        @Override
                        public void afterHookedMethod(MethodHookParam param) {
                            if (!enabled || !showPreviewButton) return;
                            final ContentPreviewViewer viewer = (ContentPreviewViewer) param.thisObject;
                            AndroidUtilities.runOnUIThread(new Runnable() {
                                @Override
                                public void run() {
                                    try {
                                        addPreviewButton(viewer);
                                    } catch (Throwable t) {
                                        Log.e(TAG, "addPreviewButton error", t);
                                    }
                                }
                            }, 40);
                        }
                    }));
                }
            }
        } catch (Throwable t) {
            Log.e(TAG, "hookContentPreviewViewer failed", t);
        }
    }

    private static void hookMediaDataController() {
        try {
            for (Method m : MediaDataController.class.getDeclaredMethods()) {
                if ("addRecentSticker".equals(m.getName())) {
                    hooks.add(XposedBridge.hookMethod(m, new XC_MethodHook() {
                        @Override
                        public void beforeHookedMethod(MethodHookParam param) {
                            if (!enabled || !blockRecents) return;
                            if (param.args == null || param.args.length < 3) return;
                            TLRPC.Document doc = (TLRPC.Document) param.args[2];
                            if (doc == null) return;
                            TLRPC.InputStickerSet set = MessageObject.getInputStickerSet(doc);
                            if (set == null || set instanceof TLRPC.TL_inputStickerSetEmpty) {
                                param.setResult(null);
                            }
                        }
                    }));
                }
            }
        } catch (Throwable t) {
            Log.e(TAG, "hookMediaDataController failed", t);
        }
    }

    private static void addPreviewButton(final ContentPreviewViewer viewer) {
        if (viewer == null || !enabled || !showPreviewButton) return;
        try {
            if (fCurrentDocument == null) initReflection();
            if (fCurrentDocument == null) return;

            final TLRPC.Document document = (TLRPC.Document) fCurrentDocument.get(viewer);
            if (document == null) return;

            if (!MessageObject.isStickerDocument(document) &&
                !MessageObject.isAnimatedStickerDocument(document, true) &&
                !MessageObject.isVideoSticker(document)) {
                return;
            }

            ActionBarPopupWindow popupWindow = fPopupWindow != null ? (ActionBarPopupWindow) fPopupWindow.get(viewer) : null;
            View menuView = popupWindow != null ? popupWindow.getContentView() : null;
            if (menuView == null && fPopupLayout != null) {
                menuView = (View) fPopupLayout.get(viewer);
            }
            if (!(menuView instanceof ViewGroup)) return;
            final ViewGroup menu = (ViewGroup) menuView;

            if (menu.findViewWithTag("unlinked_stickers_btn") != null) {
                return;
            }

            Theme.ResourcesProvider resProvider = fResourcesProvider != null ? (Theme.ResourcesProvider) fResourcesProvider.get(viewer) : null;

            int icon = 0;
            try {
                Context ctx = menu.getContext();
                icon = ctx.getResources().getIdentifier("msg_send", "drawable", ctx.getPackageName());
            } catch (Throwable ignored) {}
            if (icon == 0) {
                try {
                    Class<?> rDraw = Class.forName("org.telegram.messenger.R$drawable");
                    Field f = rDraw.getField("msg_send");
                    icon = f.getInt(null);
                } catch (Throwable ignored) {}
            }

            final boolean sendWithPack = (mode == 0);
            String text = getBtnText(sendWithPack);

            ActionBarMenuSubItem item = ActionBarMenuItem.addItem(menu, icon, text, false, resProvider);
            if (item != null) {
                item.setTag("unlinked_stickers_btn");
                item.setOnClickListener(new View.OnClickListener() {
                    @Override
                    public void onClick(View v) {
                        doSendFromPreview(viewer, document, sendWithPack);
                    }
                });
            }
        } catch (Throwable t) {
            Log.e(TAG, "addPreviewButton failed", t);
        }
    }

    private static void doSendFromPreview(ContentPreviewViewer viewer, TLRPC.Document document, boolean sendWithPack) {
        try {
            long docId = document.id;
            if (sendWithPack) {
                bypassSendWithPack.add(docId);
            } else {
                bypassSendWithoutPack.add(docId);
            }

            ContentPreviewViewer.ContentPreviewViewerDelegate delegate = null;
            if (fDelegate != null) {
                delegate = (ContentPreviewViewer.ContentPreviewViewerDelegate) fDelegate.get(viewer);
            }

            if (delegate != null && delegate.canSendSticker()) {
                String query = fCurrentQuery != null ? (String) fCurrentQuery.get(viewer) : null;
                Object parent = fParentObject != null ? fParentObject.get(viewer) : null;
                delegate.sendSticker(document, query, parent, true, 0, 0);
            } else {
                long dialogId = delegate != null ? delegate.getDialogId() : 0;
                if (dialogId == 0) {
                    BaseFragment last = LaunchActivity.getLastFragment();
                    if (last instanceof ChatActivity) {
                        dialogId = ((ChatActivity) last).getDialogId();
                    }
                }
                if (dialogId != 0) {
                    int account = UserConfig.selectedAccount;
                    SendMessagesHelper.getInstance(account).sendSticker(
                        document, null, dialogId, null, null, null, null, null, true, 0, 0, false, null, null, 0, 0, null
                    );
                }
            }
        } catch (Throwable t) {
            Log.e(TAG, "doSendFromPreview error", t);
        } finally {
            try {
                viewer.closeWithMenu();
            } catch (Throwable t) {
                try {
                    Method mDismiss = ContentPreviewViewer.class.getDeclaredMethod("dismissPopupWindow");
                    mDismiss.setAccessible(true);
                    mDismiss.invoke(viewer);
                } catch (Throwable ignored) {}
            }
        }
    }

    private static String getBtnText(boolean sendWithPack) {
        boolean isRu = false;
        try {
            Locale loc = LocaleController.getInstance().getCurrentLocale();
            if (loc != null && "ru".equalsIgnoreCase(loc.getLanguage())) {
                isRu = true;
            }
        } catch (Throwable ignored) {}
        if (sendWithPack) {
            return isRu ? "Отправить со стикерпаком" : "Send with sticker pack";
        } else {
            return isRu ? "Отправить без стикерпака" : "Send without sticker pack";
        }
    }
}
