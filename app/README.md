# SmartCart mobile application

Stage 4 — Manager UI and mock monitoring repository. Run Flutter commands here:

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
- `lib/features/auth/data/in_memory_auth_repository.dart`: temporary demo implementation.
- `lib/features/auth/controllers/auth_controller.dart`: session, loading, safe errors, and duplicate-submission protection using ChangeNotifier.
- `lib/features/auth/screens/`: login and the preserved Stage 2 placeholder source.
- `lib/features/customer/`: Customer cart models, repository, controller, screen, and reusable item/bill widgets.
- `lib/features/launch/launch_screen.dart`: preserved Stage 1 foundation screen, no longer the initial route.

UI → controller → repository abstraction → in-memory implementation.
No additional state-management or routing package is used. Authentication state selects the root screen, so logout cannot leave a protected screen in the back stack.

## Development authentication only

Use a made-up email (for example `demo@example.test`) and any nonblank made-up password. This is not credential verification and must not be used as production authentication. The visible development selector chooses Customer or Manager through the repository's optional capability; roles are never derived from an email or username.

The demo simulates a short delay, retains only the role in memory, and loses the session on restart. Passwords are not logged, persisted, or stored in the repository/controller; the form clears the password when submitting. Invalid repository input can produce AuthFailure. Injected repositories can simulate other failures; users see safe retry messages.

During Stage 6, supply a Firebase-backed AuthRepository at the app composition point and omit DevelopmentRoleSelection. The login form and placeholders need no Firebase-specific code. Session restoration and external authentication events/token expiration must be added to the controller/repository lifecycle during that integration. This stage reads the repository's current session at startup and manages sign-in/sign-out changes only. No Firebase user schema or backend role contract is defined here.

## Customer cart

Customer login opens Customer Cart with session status, repository-provided items, and a Current Bill summary on the same scrollable screen. Manager opens its monitoring dashboard. Both keep authentication-driven logout.

Customer UI → CartController (ChangeNotifier) → CartRepository → MockCartRepository. The Customer controller is disposed on logout; pending loads cannot restore a signed-out view. Inject a different CartRepository through SmartCartApp to replace the data source without changing widgets.

CustomerCart, CartItem, and CartProduct are client presentation models, independent of backend session or recognition code. Money uses integer paise, formatted separately. The mock alone derives sample line/subtotal/total values. Widgets consume explicit repository totals; authoritative billing remains the backend's responsibility. Item count means total units, not distinct product lines. No tax or discount rules are invented.

Names and prices are development fixtures, not a permanent product catalogue. The default mock demonstrates six lines/eight units and ₹130.00. MockCartScenario also supports empty, noSession, and failure fixtures for injection/tests. Loading and safe error/retry states are handled by the controller. The UI clearly labels demo data and makes no live-connectivity claim.

Firebase is not connected. There is no pairing, live subscription, checkout, cart editing, or backend integration. Cart data loads on entering the Customer screen and on error retry; realtime updates and user/session binding await the backend contract.

## Manager monitoring

Manager UI → ManagerController (ChangeNotifier) → ManagerRepository → MockManagerRepository. The read-only dashboard shows repository-supplied active carts, current alerts, and recent activity. Counts reflect the supplied collections; bill amounts reuse the Customer rupee formatter without recalculation. No detail/editing view is included.

All fixtures are development-only. Alerts and event labels represent backend-originated concepts: Flutter displays messages and performs no theft detection, event classification, or sensor analysis. Firebase remains disconnected. The mock supports populated, empty, and failure scenarios; the controller supports loading, safe errors, retry, duplicate-request protection, and disposal on logout.

Proposed client requirements for later contract review: session display ID, supplied status, item count, authoritative total in paise, update timestamp; alert ID, severity/status labels, message, timestamp and related session; event ID, label, message, timestamp and related session. These are not Firebase paths or a finalized schema. Repository collections must be scoped/ordered appropriately by the future adapter. Timestamps display local date/time; data is a snapshot loaded on entry or retry, not live monitoring.

## Validation

```sh
dart format .
flutter analyze
flutter test
flutter build apk --debug
```

Tests cover authentication and logout, preserved Stage 1 content, cart states/retry, item and bill rendering, integer money, repository/controller lifecycle, and smaller screens with increased text scaling.

Android is the development target. iOS scaffolding remains unvalidated. Generated app identifiers and launcher icons are development defaults. This folder belongs to the parent smart-cart Git repository.
