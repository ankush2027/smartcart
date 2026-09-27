# SmartCart mobile application

Stage 9 — Firebase Authentication, authenticated customer Cart ID pairing, live customer carts and live manager RTDB monitoring. See [Stage 9 manager integration](STAGE9_MANAGER_LIVE.md). See [Stage 8 setup and physical-device checkpoint](STAGE8_PAIRING.md). Run Flutter commands here:

```sh
flutter pub get
flutter run
```

## Architecture

- `lib/main.dart`: application entry point.
- `lib/app.dart`: creates and disposes the authentication controller; accepts an injected repository.
- `lib/core/theme/app_theme.dart`: existing Material 3 theme.
- `lib/core/routes/app_routes.dart`: a single authentication-controlled root route.
- `lib/features/auth/models/auth_user.dart`: minimal application user and Customer/Manager roles.
- `lib/features/auth/data/auth_repository.dart`: sign-in, sign-out, and current-session contract; optional development role-selection capability.
- `lib/features/auth/data/firebase_auth_repository.dart`: production email/password sign-in, token-claim roles, session restoration and token events.
- `lib/features/auth/data/in_memory_auth_repository.dart`: explicitly injected demo/test implementation only.
- `lib/features/auth/controllers/auth_controller.dart`: session, loading, safe errors, and duplicate-submission protection using ChangeNotifier.
- `lib/features/auth/screens/`: login and the preserved Stage 2 placeholder source.
- `lib/features/customer/`: Customer cart models, repository, controller, screen, and reusable item/bill widgets.
- `lib/features/launch/launch_screen.dart`: preserved Stage 1 foundation screen, no longer the initial route.

UI → controller → repository abstraction → FirebaseAuthRepository → Firebase Authentication.
No additional state-management or routing package is used. Authentication state selects the root screen, so logout cannot leave a protected screen in the back stack.

## Firebase authentication

The default app uses FirebaseAuthRepository after Firebase Core initialization in main.dart. Email/password sign-in is enabled for project `smartcart-16807`, Android ID `com.smartcart.app`. Only `firebase_auth` was added as a direct dependency in Stage 6.

AuthUser stores the exact Firebase UID and the resolved role. Only an ID token custom claim of `role: manager` grants the manager role; all other claim values default to customer. No email, display name, route or local selector grants production manager access. The login screen hides its development selector because the Firebase repository does not implement DevelopmentRoleSelection. InMemoryAuthRepository remains available only through explicit dependency injection for tests/demos.

The repository listens to idTokenChanges for the initial persisted session, user changes, sign-out and refreshed claims. Async claim results are discarded after a newer session event or operation. Claim lookup failures fail closed. The controller subscribes and disposes its listener, exposes loading and safe errors, clears stale users on failed login, and ignores duplicate submissions. Successful logout clears both Firebase and app state; a failed logout displays a retry error instead of claiming success. The single guarded root selects login/customer/manager with no protected back stack. Screens are keyed by UID so user switches dispose feature state.

Firebase calls and errors stay in the data layer. Passwords are cleared from the form on submission and are not logged or stored by the app. Common credential, email, disabled-user, network and throttling errors have safe messages.

The UID is an authentication identity, never a cart ID or session ID. The Stage 7 customer repository discovers owned sessions through `orderByChild("customer_uid").equalTo(auth.uid)`. Client routing is presentation control. Deployed database security rules enforce actual data access.

### Real-project manual checks still required

Automated tests use fake Firebase objects and require no live project. No accounts are created by this implementation. Use authorized, existing Email/Password customer and manager test accounts, or have an administrator provision them separately.

1. On Android, verify valid customer sign-in, incorrect password, logout, and restart restoration against `smartcart-16807`.
2. Verify a manager account with the trusted `role: manager` claim opens the manager dashboard. Verify the same email without that claim opens the customer area.
3. Switch manager → logout → customer; confirm role and prior screen state are cleared. Check disabled account, offline/network and throttling behavior where practical.
4. Verify claim removal after a fresh token/sign-in. Admin claim updates propagate when a new ID token is issued; they are not an immediate push. Sign out and sign back in after an administrator changes claims. Remote disable/delete likewise is not an immediate client stream notification.

A trusted Admin SDK/backend/admin tool must assign manager custom claims. No claim-assignment mechanism is provided in Flutter; supplying one is an external administrative requirement. Never place service-account credentials in the app. See [Firebase token events](https://firebase.google.com/docs/auth/flutter/start) and [custom claims](https://firebase.google.com/docs/auth/admin/custom-claims).

## Customer cart

Customer login opens Customer Cart with session status, repository-provided items, and a Current Bill summary on the same scrollable screen. Manager opens its monitoring dashboard. Both keep authentication-driven logout.

Customer UI → CartController → FirebaseCartRepository → UID-scoped RTDB stream. With no active session, Cart ID entry calls CartPairingService → fresh Firebase ID token → authenticated POST /pair-cart. Backend SQLite owns assignment; HTTP acknowledges it, and RTDB supplies the displayed cart. No cart is synthesized from the HTTP response.

The controller maintains one stream subscription, waits up to 20 seconds for discovery/synchronization, and offers safe retry. Logout or UID changes dispose the screen, input, controller and listener; late results cannot restore its state. Logout does not unpair. Active empty carts remain valid carts. All bill amounts retain Stage 7 integer-paise mapping and supplied authoritative totals.

MockCartRepository is available through explicit injection for offline tests/demos. Production customer and manager data are live. There is no checkout, cart editing, QR scanning or client RTDB write API.

Configure pairing for a physical Android phone on the same LAN:

```sh
flutter run --dart-define=SMARTCART_API_BASE_URL=http://<EDGE-LAN-IP>:5000
```

Replace the placeholder locally. The phone's localhost refers to the phone. Missing/invalid configuration produces a controlled error, with no fallback. HTTP is enabled only for debug development builds; release requires HTTPS. See the Stage 8 guide for backend setup and manual verification.

## Manager monitoring

Manager UI → ManagerController → FirebaseManagerRepository → authorized `/sessions` live stream. Access requires the Stage 6 trusted `role: manager` custom claim and deployed RTDB rules. Production has no mock fallback; MockManagerRepository remains explicitly injectable for demos/tests.

The dashboard shows active and completed sessions, cart/customer identity, start/end times, supplied bill items and authoritative totals, plus neutral ADD/REMOVE activity from event logs. Active/completed status derives only from end_time. No alert fields exist in this contract, so production omits the demo alert section. All money remains integer paise; no Firebase writes or theft decisions are added.

Subscriptions bind to the current manager identity and are cancelled on logout/screen disposal. Retry opens a fresh read; old callbacks cannot restore another identity's data. See [Stage 9 architecture, validation and physical-device checkpoint](STAGE9_MANAGER_LIVE.md) for mapping details, empty/error states and limitations.

## Validation

```sh
dart format .
flutter analyze
flutter test
flutter build apk --debug
```

Tests cover authentication and logout, preserved Stage 1 content, cart states/retry, item and bill rendering, integer money, repository/controller lifecycle, and smaller screens with increased text scaling.

Android is the development target. iOS scaffolding remains unvalidated. Android uses the approved application ID; launcher icons remain development defaults. This folder belongs to the parent smart-cart Git repository.

## Stage 7 repository foundation

Read-only Firebase customer/manager adapters and offline mapping/listener tests are implemented. Stage 8 wires the customer adapter to production; Stage 9 wires the manager adapter. The earlier contract gaps are resolved by backend commit ca522a9 and its user-confirmed deployed rules. Zero active sessions is normal before pairing; multiple active sessions are an integrity conflict. Manager authorization uses role == "manager". Cart ID pairing is now implemented in Stage 8. See [Stage 7 contract analysis and integration requirements](STAGE7_RTDB.md) before Stage 8/9. No backend data or Firebase rules were modified.
