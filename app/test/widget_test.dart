import 'package:smartcart/features/manager/data/mock_manager_repository.dart';
import 'package:smartcart/features/customer/data/mock_cart_repository.dart';

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/app.dart';
import 'package:smartcart/features/auth/data/auth_repository.dart';
import 'package:smartcart/features/auth/data/in_memory_auth_repository.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';
import 'package:smartcart/features/launch/launch_screen.dart';

Future<void> enterCredentials(WidgetTester tester) async {
  await tester.enterText(
    find.widgetWithText(TextFormField, 'Email'),
    'demo@example.test',
  );
  await tester.enterText(
    find.widgetWithText(TextFormField, 'Password'),
    'demo-only',
  );
}

class PendingRepository implements AuthRepository {
  final result = Completer<AuthUser>();
  @override
  AuthUser? get currentUser => null;
  @override
  Future<AuthUser> signIn({required String email, required String password}) =>
      result.future;
  @override
  Future<void> signOut() async {}
}

void main() {
  testWidgets('unauthenticated root shows login and validates input', (
    tester,
  ) async {
    await tester.pumpWidget(
      SmartCartApp(
        managerRepository: MockManagerRepository(),
        cartRepository: MockCartRepository(),
        authRepository: InMemoryAuthRepository(),
      ),
    );
    expect(find.text('Welcome Back'), findsOneWidget);
    expect(find.text('Development demo only'), findsOneWidget);
    await tester.ensureVisible(find.text('Sign In'));
    await tester.tap(find.text('Sign In'));
    await tester.pumpAndSettle();
    expect(find.text('Please enter a valid email address.'), findsOneWidget);
    expect(find.text('Password is required.'), findsOneWidget);
    expect(find.text('Logout'), findsNothing);
  });

  for (final role in UserRole.values) {
    testWidgets('${role.label} sign-in and logout are driven by auth state', (
      tester,
    ) async {
      await tester.pumpWidget(
        SmartCartApp(
          managerRepository: MockManagerRepository(),
          cartRepository: MockCartRepository(),
          authRepository: InMemoryAuthRepository(delay: Duration.zero),
        ),
      );
      if (role == UserRole.manager) {
        await tester.tap(find.byType(DropdownButtonFormField<UserRole>));
        await tester.pumpAndSettle();
        await tester.tap(find.text('Manager').last);
        await tester.pumpAndSettle();
      }
      await enterCredentials(tester);
      await tester.ensureVisible(find.text('Sign In'));
      await tester.tap(find.text('Sign In'));
      await tester.pumpAndSettle();
      if (role == UserRole.customer) {
        expect(find.text('Customer Cart'), findsOneWidget);
      } else {
        expect(find.text('Manager Dashboard'), findsOneWidget);
      }
      expect(find.text('Welcome Back'), findsNothing);
      await tester.scrollUntilVisible(find.text('Logout'), 300);
      await tester.tap(find.text('Logout'));
      await tester.pumpAndSettle();
      expect(find.text('Welcome Back'), findsOneWidget);
      expect(find.text('Logout'), findsNothing);
      expect(
        tester
            .widget<TextFormField>(
              find.widgetWithText(TextFormField, 'Password'),
            )
            .controller!
            .text,
        isEmpty,
      );
      final context = tester.element(find.text('Welcome Back'));
      expect(Navigator.of(context).canPop(), isFalse);
    });
  }

  testWidgets('password is obscured and can be revealed', (tester) async {
    await tester.pumpWidget(
      SmartCartApp(
        managerRepository: MockManagerRepository(),
        cartRepository: MockCartRepository(),
        authRepository: InMemoryAuthRepository(),
      ),
    );
    final password = find.widgetWithText(TextField, 'Password');
    expect(tester.widget<TextField>(password).obscureText, isTrue);
    await tester.tap(find.byTooltip('Show password'));
    await tester.pump();
    expect(tester.widget<TextField>(password).obscureText, isFalse);
  });

  testWidgets(
    'pending sign-in disables submission and safely displays failure',
    (tester) async {
      final repository = PendingRepository();
      await tester.pumpWidget(
        SmartCartApp(
          managerRepository: MockManagerRepository(),
          cartRepository: MockCartRepository(),
          authRepository: repository,
        ),
      );
      expect(find.text('Temporary test role'), findsNothing);
      await enterCredentials(tester);
      await tester.ensureVisible(find.text('Sign In'));
      await tester.tap(find.text('Sign In'));
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(
        tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
        isNull,
      );
      repository.result.completeError(
        StateError('private implementation detail'),
      );
      await tester.pumpAndSettle();
      expect(find.text('Unable to sign in. Please try again.'), findsOneWidget);
      expect(
        find.textContaining('private implementation detail'),
        findsNothing,
      );
      expect(find.text('Sign In'), findsOneWidget);
    },
  );

  testWidgets('login supports small screens, large text, and keyboard inset', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 480);
    tester.view.devicePixelRatio = 1;
    tester.view.viewInsets = const FakeViewPadding(bottom: 180);
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetViewInsets);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    await tester.pumpWidget(
      SmartCartApp(
        managerRepository: MockManagerRepository(),
        cartRepository: MockCartRepository(),
        authRepository: InMemoryAuthRepository(),
      ),
    );
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Sign In'));
    expect(find.text('Sign In').hitTestable(), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Stage 1 launch screen retains its foundation content', (
    tester,
  ) async {
    await tester.pumpWidget(const MaterialApp(home: LaunchScreen()));
    for (final text in [
      'SmartCart',
      'Smart Shopping Cart',
      'Automatic Billing System',
      'Mobile Application',
      'System Ready',
    ]) {
      expect(find.text(text), findsOneWidget);
    }
    expect(tester.takeException(), isNull);
  });
}
