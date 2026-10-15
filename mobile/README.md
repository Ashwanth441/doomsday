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
- Native push uses Apple Push Notification service (APNs) on iOS and Firebase Cloud Messaging (FCM) on Android. Device tokens are tied to the signed-in account and removed on sign-out.
- Private photos and documents are fetched with the signed-in user's authorization. On native devices, a user tapping a document receives the system share/open sheet.
- The app UI is bundled locally; task, offer, chat, profile and account data still require the backend connection.

## Configure native push

The app-side registration and server delivery paths are implemented, but background alerts remain off until the provider credentials are added. Keep private keys in Render environment variables or a secret manager; never commit them or paste them into chat.

### iPhone

1. Enroll the Apple account that owns the Doomsday team in the Apple Developer Program. A free Personal Team cannot create the Push Notifications provisioning profile needed for APNs. After enrollment, enable Push Notifications for `in.doomsday.marketplace` in Xcode and apply the included debug/release entitlements.
2. In Certificates, Identifiers & Profiles → Keys, create an APNs key and save the `.p8` file once. Record its Key ID and your Team ID.
3. Add these Render environment variables, then redeploy:
   - `APNS_TEAM_ID`: Apple Team ID
   - `APNS_KEY_ID`: APNs key ID
   - `APNS_BUNDLE_ID`: `in.doomsday.marketplace`
   - `APNS_AUTH_KEY_BASE64`: base64-encoded contents of the `.p8` key
   - `APNS_USE_SANDBOX`: `true` for an Xcode development install; `false` for TestFlight/App Store builds
4. Install the rebuilt iOS app, sign in, then open Profile → Enable push notifications and allow the iOS prompt.

The `.p8` file is downloadable only once from Apple. Keep the original in a password manager or secure backup. The APNs private key belongs only on the server; the iOS app receives only its device token.

### Android

1. Create/register the Android app in Firebase using the same application ID, `in.doomsday.marketplace`, then download `google-services.json` into `android/app/`.
2. Give the Firebase server service account permission to send Firebase Cloud Messaging messages. Store its complete JSON key in Render as `FCM_SERVICE_ACCOUNT_JSON` and redeploy.
3. Run Capacitor sync, build/install the Android app, sign in, then use Profile → Enable push notifications.

The server reports iOS and Android push readiness independently through `/api/config`; the profile action becomes available only when its platform is configured. Existing in-app notifications continue to work without either provider.

## Before a public store release

The native projects are a build starting point. These launch requirements remain:

- **Durable backend:** Render Free has an ephemeral filesystem, so the current SQLite database and uploaded photos/files can be lost. Move to persistent storage or a managed database/object-storage setup before real customer data.
- **Marketplace payouts:** Razorpay Route approval and tasker linked-account onboarding are still required before marketplace payouts can be enabled.
- **Native push:** add the APNs and/or FCM credentials above, then verify delivery on physical devices using development and production signing separately before promising background push alerts.
- **Digital deliverables:** Doomsday's student tasks can sell downloadable PPT/PDF deliverables. Review both store billing policies and implement Apple/Google billing for any digital content/service where required, or disable those paid digital flows in store builds. Razorpay checkout is suitable for eligible physical/offline services, subject to each store's policy.
- **Store materials:** provide a public privacy policy, terms, support contact, moderation/contact details, app screenshots, accurate privacy/data-safety disclosures, and a store description.
- **Production accounts:** the owner must set signing identities, complete store account verification and agreements, and submit the signed builds. Signing credentials should never be committed to this repository.

Do not submit this first package as a production release until the items above are resolved and the customer and tasker flows have been exercised on real iOS and Android devices against a durable production backend.
