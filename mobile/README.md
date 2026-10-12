# Doomsday mobile apps

This project packages the existing Doomsday marketplace for iOS and Android with Capacitor. The web screens and marketplace API are bundled separately: the UI is part of the install, and it connects to the HTTPS Doomsday server. It does not load the live website as its app screen.

## Build the native projects

1. Install Node.js 22+ and pnpm, then run `pnpm install` from the repository root.
2. Confirm `appId` in `capacitor.config.json` is an identifier you control. `in.doomsday.marketplace` is a provisional value.
3. Run `pnpm run assets:source`, then `pnpm run assets:sync-native` to update the Android and iOS icons and splash screens.
4. Run `pnpm run cap:sync` to package the latest app screens and sync Capacitor plugins.
5. Run `pnpm run cap:android` to open Android Studio or `pnpm run cap:ios` to open Xcode.

### Android Play upload signing

The Android release bundle is unsigned unless all four signing values are supplied to Gradle. Keep the upload keystore and its passwords outside source control, back them up in a password manager, and use the same upload key for later updates. Set these environment variables in the local build environment before running `./gradlew bundleRelease` from `android/`:

- `DOOMSDAY_UPLOAD_KEYSTORE`: absolute path to the upload `.jks` file
- `DOOMSDAY_UPLOAD_STORE_PASSWORD`: keystore password
- `DOOMSDAY_UPLOAD_KEY_ALIAS`: key alias
- `DOOMSDAY_UPLOAD_KEY_PASSWORD`: key password

The bundle is written to `android/app/build/outputs/bundle/release/app-release.aab`. Do not upload it until it has a verified upload-key signature and the final Android application ID has been chosen; `in.doomsday.marketplace` is still provisional.

Capacitor 8 needs Node.js 22+, Android Studio 2025.2.1+ with an Android SDK, and Xcode 26+ on macOS for iOS builds. Android Studio/Xcode are needed to create signed store builds and test on devices.

The default API is `https://doomsday-gm5k.onrender.com`. To build for another API host, set `DOOMSDAY_API_BASE` to its HTTPS origin before `pnpm run cap:sync`. The backend must allow these exact origins in `MOBILE_ALLOWED_ORIGINS`:

```text
capacitor://localhost,https://localhost
```

The Render blueprint now sets that allowlist. It only enables API access from Doomsday's iOS/Android webviews, not arbitrary websites.

## Native behavior

- Location is requested only when a user taps a location action.
- External map links open in the platform browser sheet.
- Session tokens are stored in iOS Keychain and Android Keystore-backed storage.
- Private photos and documents are fetched with the signed-in user's authorization. On native devices, a user tapping a document receives the system share/open sheet.
- The app UI is bundled locally; task, offer, chat, profile and account data still require the backend connection.

## Before a public store release

The native projects are a build starting point. These launch requirements remain:

- **Durable backend:** Render Free has an ephemeral filesystem, so the current SQLite database and uploaded photos/files can be lost. Move to persistent storage or a managed database/object-storage setup before real customer data.
- **Marketplace payouts:** Razorpay Route approval and tasker linked-account onboarding are still required before marketplace payouts can be enabled.
- **Native push:** the current web push setup is not APNs/FCM. Add Apple/Google push credentials and native notification handling before promising background push alerts.
- **Digital deliverables:** Doomsday's student tasks can sell downloadable PPT/PDF deliverables. Review both store billing policies and implement Apple/Google billing for any digital content/service where required, or disable those paid digital flows in store builds. Razorpay checkout is suitable for eligible physical/offline services, subject to each store's policy.
- **Store materials:** provide a public privacy policy, terms, support contact, moderation/contact details, app screenshots, accurate privacy/data-safety disclosures, and a store description.
- **Production accounts:** the owner must set signing identities, complete store account verification and agreements, and submit the signed builds. Signing credentials should never be committed to this repository.

Do not submit this first package as a production release until the items above are resolved and the customer and tasker flows have been exercised on real iOS and Android devices against a durable production backend.
