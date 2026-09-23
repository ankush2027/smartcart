# SmartCart mobile application

Stage 3 — Customer UI and mock cart repository. Run Flutter commands here:

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
- `lib/features/auth/screens/`: login and the retained Manager placeholder.
- `lib/features/customer/`: Customer cart models, repository, controller, screen, and reusable item/bill widgets.
- `lib/features/launch/launch_screen.dart`: preserved Stage 1 foundation screen, no longer the initial route.

UI → controller → repository abstraction → in-memory implementation.
No additional state-management or routing package is used. Authentication state selects the root screen, so logout cannot leave a protected screen in the back stack.

## Development authentication only

Use a made-up email (for example `demo@example.test`) and any nonblank made-up password. This is not credential verification and must not be used as production authentication. The visible development selector chooses Customer or Manager through the repository's optional capability; roles are never derived from an email or username.

The demo simulates a short delay, retains only the role in memory, and loses the session on restart. Passwords are not logged, persisted, or stored in the repository/controller; the form clears the password when submitting. Invalid repository input can produce AuthFailure. Injected repositories can simulate other failures; users see safe retry messages.

During Stage 6, supply a Firebase-backed AuthRepository at the app composition point and omit DevelopmentRoleSelection. The login form and placeholders need no Firebase-specific code. Session restoration and external authentication events/token expiration must be added to the controller/repository lifecycle during that integration. This stage reads the repository's current session at startup and manages sign-in/sign-out changes only. No Firebase user schema or backend role contract is defined here.

## Customer cart

Customer login opens Customer Cart with session status, repository-provided items, and a Current Bill summary on the same scrollable screen. Manager retains its Stage 2 placeholder. Both keep authentication-driven logout.

Customer UI → CartController (ChangeNotifier) → CartRepository → MockCartRepository. The Customer controller is disposed on logout; pending loads cannot restore a signed-out view. Inject a different CartRepository through SmartCartApp to replace the data source without changing widgets.

CustomerCart, CartItem, and CartProduct are client presentation models, independent of backend session or recognition code. Money uses integer paise, formatted separately. The mock alone derives sample line/subtotal/total values. Widgets consume explicit repository totals; authoritative billing remains the backend's responsibility. Item count means total units, not distinct product lines. No tax or discount rules are invented.

Names and prices are development fixtures, not a permanent product catalogue. The default mock demonstrates six lines/eight units and ₹130.00. MockCartScenario also supports empty, noSession, and failure fixtures for injection/tests. Loading and safe error/retry states are handled by the controller. The UI clearly labels demo data and makes no live-connectivity claim.

Firebase is not connected. There is no pairing, live subscription, checkout, cart editing, Manager dashboard, or backend integration. Cart data loads on entering the Customer screen and on error retry; realtime updates and user/session binding await the backend contract.

## Validation

```sh
dart format .
flutter analyze
flutter test
flutter build apk --debug
```

Tests cover authentication and logout, preserved Stage 1 content, cart states/retry, item and bill rendering, integer money, repository/controller lifecycle, and smaller screens with increased text scaling.

Android is the development target. iOS scaffolding remains unvalidated. Generated app identifiers and launcher icons are development defaults. This folder belongs to the parent smart-cart Git repository.
