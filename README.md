# Doomsday

Doomsday is an installable, mobile-first task marketplace built for users across India. It includes the web app, a same-origin JSON API, a normalized SQLite database, real Twilio Verify authentication, Razorpay Checkout and Route integration points, live in-app chat updates, push-notification support, and an admin area.

There is no Tamil Nadu-only location restriction. Local task discovery works with GPS coordinates anywhere in India, or by the city entered in a user's profile when GPS is unavailable. Online student study-support tasks can be discovered by users nationwide.

## Run it locally

Use Python 3.12 or newer. No framework install is needed for the app itself.

```sh
cp .env.example .env
python3 server.py
```

Open `http://127.0.0.1:8787`. Until credentials are configured, the sign-in and payment flows stay disabled; the app does not issue pretend OTPs or pretend payments. Set `HOST=0.0.0.0` when running in a container.

## Provider setup

### Phone verification

Create a Twilio Verify service, then set `TWILIO_API_KEY`, `TWILIO_API_SECRET`, and `TWILIO_VERIFY_SERVICE_SID`. Do not put provider secrets in the browser bundle. Set `ADMIN_BOOTSTRAP_PHONE` to the first admin's E.164 number; it is disabled when blank. Set `COOKIE_SECURE=true` behind HTTPS. After that phone completes real verification, it receives the first admin account.

### Payments and payouts

Set the Razorpay key ID, key secret, and webhook secret. Enable Razorpay Route for the marketplace, configure the webhook URL as `https://your-domain.example/api/webhooks/razorpay`, and subscribe to `payment.captured`, `payment.failed`, `refund.processed`, `refund.failed`, and Route transfer events supported by your Razorpay account. The server verifies Checkout signatures and fetches the payment from Razorpay before changing it to paid. It calculates the tasker's net payout from the admin-configured fee, and only requests the Route transfer after the customer confirms the task. The server locks uncertain provider responses for admin reconciliation instead of retrying a transfer or refund that may already have been accepted.

Razorpay Route activation and tasker onboarding are account-level requirements. After Razorpay has approved a tasker's linked account and payout details, an admin can associate its `acc_…` ID in the dashboard. Doomsday never collects card, CVV, or UPI credentials. Checkout and payout availability stay blocked until the relevant provider configuration and linked account exist. Settlement timing is controlled by Razorpay and the linked-account configuration; the app does not describe this flow as escrow or promise an instant bank credit.

### Push notifications

Install the optional `pywebpush` dependency from `requirements.txt`, generate VAPID keys, and set `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, and `VAPID_SUBJECT`. In-app notifications and live events work without push credentials. Browser push requires HTTPS and user consent.

## Student study-support tasks

People can enable **I am currently a student** in profile setup or profile editing. Only those accounts can post the Student Study Support category; the server also enforces this. This is self-declared and is not student-ID verification. These tasks are for tutoring, feedback, practice materials, and study aids, not completing graded work for submission as a student's own.

After a customer chooses an offer, private task chat opens for both participants. Only the chosen tasker can submit or replace the deliverable. The tasker uploads the original plus a limited or watermarked PDF preview; the student can view the preview in the app. The student pays the accepted offer price plus the configurable platform fee, so the tasker receives the full offer price. The original download, and tasker-sent documents in chat, remain server-locked until a signed Razorpay Route webhook confirms payout settlement. This is a provider-controlled marketplace transfer, not escrow. Razorpay must approve Route and the tasker's linked account before payments can run. Screenshots and copying of an on-screen preview cannot be fully prevented.

Chat supports one text message or one photo/document attachment per send. Supported documents are PDF, PPTX, DOCX, and XLSX; images are JPG, PNG, or WebP. Each upload is limited to 8 MB and requires an assigned-task conversation.

## Database and files

The default database is `data/doomsday.sqlite3`; set `DOOMSDAY_DB_PATH` to place it on a persistent volume. For a managed PostgreSQL database, set `DATABASE_URL`; the server creates its schema on startup and keeps SQLite as the local default. PostgreSQL mode uses indexed task location bounds and server-side distance calculations. Search uses PostgreSQL text matching. Uploaded images and documents are stored in the database, so they move with a database migration.

The Render Blueprint currently uses the Free plan for a demo. Render Free services have an ephemeral filesystem, so this app's SQLite database and uploaded files can disappear whenever the service sleeps, restarts, or redeploys. A managed PostgreSQL database can keep data separate from that filesystem; Neon is one provider with a free tier, subject to its current limits and policies. Add its connection string as the Render `DATABASE_URL` secret, using the provider's pooled connection string where offered. Never paste that secret into source control or chat.

**Existing data is not copied automatically.** Before setting `DATABASE_URL` on an already-used deployment, export and migrate its users, tasks, offers, messages, payments, and file uploads. The hosted SQLite file is separate from this checkout, so this code cannot safely copy it on its own. Keep the deployment on SQLite until a database export has been saved and the PostgreSQL import has been checked. When `DATABASE_URL` is first enabled, the server creates a new, empty PostgreSQL schema.

The app supports locations throughout India, but this demo deployment is still a small single-instance service. Before inviting a large nationwide audience, move images to private object storage and use a shared event/push queue. Keep the API's server-side ownership and payment checks in place during that migration.

## Safety and operations

- Users need a verified phone before using marketplace endpoints. A single account can post tasks and send offers.
- Tasks that include obvious prohibited terms are rejected, and other safety concerns can be reported for admin review. This keyword check is an early filter, not a complete policy/moderation system; provide staffed review and publish final terms before launch.
- Admin tools cover users, tasks, reports, disputes, categories, fee settings, announcements, provider transaction records, and payout account associations.
- A dispute freezes the normal completion flow. Admins record a decision and review the underlying provider payment before handling a payout or refund. Uncertain payment-provider responses remain locked for manual reconciliation to prevent duplicate money movement.
- Put the server behind HTTPS, set `COOKIE_SECURE=true`, and only set `TRUST_PROXY=true` if the trusted proxy overwrites `X-Forwarded-For`. Use a persistent encrypted volume and backups, restrict access to `.env`, rotate provider keys, and monitor provider webhooks before inviting the public.

## Checks

Run the local API checks with:

```sh
python3 -m unittest discover -s tests -v
```

The checks exercise two-tasker offers, task assignment, address privacy, chat access, search, session permissions, blocking, and the payment-disabled path. They do not fake SMS delivery or money movement. Complete a real Twilio Verify test and Razorpay test-mode payment/Route transfer with your provider accounts before enabling live keys.

## Current scope

The project now includes the installable web app plus Capacitor iOS and Android projects. Native builds bundle the app UI, connect to the live API with a strict mobile-origin allowlist, keep mobile sessions in Keychain/Keystore-backed storage, request location only on demand, and use the native share sheet for private chat documents. Read [mobile/README.md](mobile/README.md) for build instructions and the production blockers that must be resolved before store submission. These projects are not signed or uploaded to either store.

Payments require both Razorpay activation and manual, verified Route linked-account association per tasker. SMS and push require the matching provider credentials. The public OSM map opens to the selected coordinates; users can also use GPS or enter coordinates and a manual address.
