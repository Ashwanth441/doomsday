import { Browser } from "@capacitor/browser";
import { Capacitor } from "@capacitor/core";
import { Directory, Filesystem } from "@capacitor/filesystem";
import { Geolocation } from "@capacitor/geolocation";
import { PushNotifications } from "@capacitor/push-notifications";
import { Share } from "@capacitor/share";
import { KeychainAccess, SecureStorage } from "@aparajita/capacitor-secure-storage";

const secureStoreReady = SecureStorage.setSynchronize(false)
  .then(() => SecureStorage.setKeyPrefix("doomsday_mobile_"));

window.DoomsdayNative = Object.freeze({
  isNativePlatform: () => Capacitor.isNativePlatform(),
  getPlatform: () => Capacitor.getPlatform(),
  checkPushPermissions: () => PushNotifications.checkPermissions(),
  requestPushPermissions: () => PushNotifications.requestPermissions(),
  registerPush: () => PushNotifications.register(),
  onPushToken: (callback) => PushNotifications.addListener("registration", callback),
  onPushRegistrationError: (callback) => PushNotifications.addListener("registrationError", callback),
  onPushAction: (callback) => PushNotifications.addListener("pushNotificationActionPerformed", callback),
  getSessionToken: async () => {
    await secureStoreReady;
    return (await SecureStorage.get("session_token")) || "";
  },
  setSessionToken: async (token) => {
    await secureStoreReady;
    await SecureStorage.set("session_token", token, false, false, KeychainAccess.whenUnlockedThisDeviceOnly);
  },
  removeSessionToken: async () => {
    await secureStoreReady;
    await SecureStorage.remove("session_token", false);
  },
  getCurrentPosition: async (options) => {
    const permission = await Geolocation.checkPermissions();
    if (permission.location !== "granted" && permission.coarseLocation !== "granted") {
      await Geolocation.requestPermissions();
    }
    return Geolocation.getCurrentPosition(options);
  },
  sharePrivateFile: async ({ name, data }) => {
    const safeName = String(name || "doomsday-file").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 120) || "doomsday-file";
    const path = `shared/${Date.now()}-${safeName}`;
    const file = await Filesystem.writeFile({ path, data, directory: Directory.Cache });
    try {
      await Share.share({ title: safeName, files: [file.uri], dialogTitle: "Open or save this file" });
    } finally {
      await Filesystem.deleteFile({ path, directory: Directory.Cache }).catch(() => {});
    }
  },
  openExternal: (url) => Browser.open({ url })
});
