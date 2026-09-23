# SmartCart mobile application

Stage 2 — Authentication foundation and UI. Run Flutter commands here:

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
- `lib/features/auth/screens/`: login and role destination placeholders.
- `lib/features/launch/launch_screen.dart`: preserved Stage 1 foundation screen, no longer the initial route.

UI → controller → repository abstraction → in-memory implementation.
No additional state-management or routing package is used. Authentication state selects the root screen, so logout cannot leave a protected screen in the back stack.

## Development authentication only

Use a made-up email (for example `demo@example.test`) and any nonblank made-up password. This is not credential verification and must not be used as production authentication. The visible development selector chooses Customer or Manager through the repository's optional capability; roles are never derived from an email or username.

The demo simulates a short delay, retains only the role in memory, and loses the session on restart. Passwords are not logged, persisted, or stored in the repository/controller; the form clears the password when submitting. Invalid repository input can produce AuthFailure. Injected repositories can simulate other failures; users see safe retry messages.

During Stage 6, supply a Firebase-backed AuthRepository at the app composition point and omit DevelopmentRoleSelection. The login form and placeholders need no Firebase-specific code. Session restoration and external authentication events/token expiration must be added to the controller/repository lifecycle during that integration. This stage reads the repository's current session at startup and manages sign-in/sign-out changes only. No Firebase user schema or backend role contract is defined here.

Customer and Manager destinations contain only their role, the future-stage message, and Logout. No cart, bill, dashboards, sessions, monitoring, or backend functionality is implemented.

## Validation

```sh
dart format .
flutter analyze
flutter test
flutter build apk --debug
```

Tests cover initial login, input validation, password visibility, both roles and logout, loading and safe errors, small-screen/large-text/keyboard behavior, the preserved Stage 1 content, and repository/controller behavior.

Android is the development target. iOS scaffolding remains unvalidated. Generated app identifiers and launcher icons are development defaults. This folder belongs to the parent smart-cart Git repository.
