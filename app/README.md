# SmartCart mobile application

Stage 1 — Flutter Foundation. Run Flutter commands from this directory.

```sh
flutter pub get
flutter run
```

## Structure

- `lib/main.dart`: application entry point.
- `lib/app.dart`: root Material application.
- `lib/core/theme/app_theme.dart`: central Material 3 theme.
- `lib/core/routes/app_routes.dart`: built-in named routes; only `/` exists.
- `lib/features/launch/launch_screen.dart`: initial SmartCart screen.
- `test/widget_test.dart`: launch content and small-screen accessibility checks.

As features are implemented, use UI → controllers/state → repositories → services.
Add auth, customer, manager, models, and data-layer files only when needed.
Future routes are `/login`, `/customer`, and `/manager`.

“System Ready” describes the local application foundation, not backend connectivity.
No Firebase, authentication, cart functionality, or backend integration is configured.
Android is the current development target; iOS scaffolding is included but unvalidated.
The generated `com.example.smartcart` identifier and launcher icons are development defaults.

## Validation

```sh
dart format .
flutter analyze
flutter test
flutter build apk --debug
```

This folder belongs to the parent smart-cart Git repository; it is not a separate repository.
