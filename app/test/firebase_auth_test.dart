import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/app.dart';
import 'package:smartcart/features/auth/controllers/auth_controller.dart';
import 'package:smartcart/features/auth/data/auth_repository.dart';
import 'package:smartcart/features/auth/data/firebase_auth_repository.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';

class FakeToken extends Fake implements IdTokenResult {
  FakeToken(this.claims);
  @override
  final Map<String, dynamic>? claims;
}

class FakeUser extends Fake implements User {
  FakeUser(this.uid, [this.claims]);
  @override
  final String uid;
  Map<String, dynamic>? claims;
  Completer<IdTokenResult>? pending;
  @override
  Future<IdTokenResult> getIdTokenResult([bool forceRefresh = false]) async =>
      pending == null ? FakeToken(claims) : await pending!.future;
}

class FakeCredential extends Fake implements UserCredential {
  FakeCredential(this.user);
  @override
  final User? user;
}

class FakeFirebaseAuth extends Fake implements FirebaseAuth {
  final events = StreamController<User?>.broadcast(sync: true);
  @override
  User? currentUser;
  FakeUser nextUser = FakeUser('firebase-customer-uid');
  String? failureCode;
  @override
  Stream<User?> idTokenChanges() async* {
    yield currentUser;
    yield* events.stream;
  }

  void change(User? user) {
    currentUser = user;
    events.add(user);
  }

  @override
  Future<UserCredential> signInWithEmailAndPassword({
    required String email,
    required String password,
  }) async {
    if (failureCode != null) {
      throw FirebaseAuthException(
        code: failureCode!,
        message: 'internal secret',
      );
    }
    change(nextUser);
    return FakeCredential(nextUser);
  }

  @override
  Future<void> signOut() async => change(null);
}

Future<void> flush() => Future<void>.delayed(Duration.zero);

void main() {
  late FakeFirebaseAuth firebase;
  late FirebaseAuthRepository repository;
  setUp(() {
    firebase = FakeFirebaseAuth();
    repository = FirebaseAuthRepository(firebaseAuth: firebase);
  });
  tearDown(() async {
    repository.dispose();
    await firebase.events.close();
  });

  Future<AuthUser> signIn() =>
      repository.signIn(email: 'manager@example.test', password: 'test-only');

  test('customer preserves Firebase UID and production has no role selector capability', () async {
    final user = await signIn();
    expect(user.uid, 'firebase-customer-uid');
    expect(user.role, UserRole.customer);
    expect(repository, isNot(isA<DevelopmentRoleSelection>()));
    final controller = AuthController(repository);
    await flush();
    controller.selectDevelopmentRole(UserRole.manager);
    expect(controller.supportsDevelopmentRoles, isFalse);
    expect(controller.user!.role, UserRole.customer);
    controller.dispose();
  });

  for (final claim in [
    null,
    'customer',
    'Manager',
    true,
    ['manager'],
    'manager',
  ]) {
    test(
      'only exact trusted role=manager grants manager access: $claim',
      () async {
        firebase.nextUser = FakeUser('trusted-uid', {
          'role': claim,
          'manager': true,
        });
        final user = await signIn();
        expect(
          user.role,
          claim == 'manager' ? UserRole.manager : UserRole.customer,
        );
      },
    );
  }

  test(
    'restores persisted manager, follows claim removal and external logout',
    () async {
      final user = FakeUser('restored-uid', {'role': 'manager'});
      firebase.currentUser = user;
      final controller = AuthController(repository);
      expect(controller.isBusy, isTrue);
      expect(controller.user, isNull);
      await flush();
      expect(controller.user!.uid, 'restored-uid');
      expect(controller.user!.role, UserRole.manager);
      user.claims = {};
      firebase.change(user);
      await flush();
      expect(controller.user!.role, UserRole.customer);
      firebase.change(null);
      await flush();
      expect(controller.user, isNull);
      controller.dispose();
    },
  );

  test(
    'sign-out clears role and switching users cannot retain manager',
    () async {
      final controller = AuthController(repository);
      await flush();
      firebase.nextUser = FakeUser('manager-uid', {'role': 'manager'});
      await controller.signIn(email: 'a@example.test', password: 'test');
      expect(controller.user!.role, UserRole.manager);
      await controller.signOut();
      expect(controller.user, isNull);
      expect(repository.currentUser, isNull);
      expect(firebase.currentUser, isNull);
      firebase.nextUser = FakeUser('customer-uid');
      await controller.signIn(email: 'a@example.test', password: 'test');
      expect(controller.user!.uid, 'customer-uid');
      expect(controller.user!.role, UserRole.customer);
      firebase.failureCode = 'invalid-credential';
      await controller.signIn(email: 'a@example.test', password: 'test');
      expect(controller.user, isNull);
      expect(repository.currentUser, isNull);
      expect(firebase.currentUser, isNull);
      expect(controller.error, 'Incorrect email or password.');
      controller.dispose();
    },
  );

  test(
    'late manager claim result cannot resurrect a logged-out user',
    () async {
      final pending = Completer<IdTokenResult>();
      firebase.currentUser = FakeUser('old-manager')..pending = pending;
      final controller = AuthController(repository);
      await flush();
      firebase.change(null);
      await flush();
      pending.complete(FakeToken({'role': 'manager'}));
      await flush();
      expect(controller.user, isNull);
      expect(repository.currentUser, isNull);
      controller.dispose();
    },
  );

  test(
    'claim lookup failure clears prior manager and signs Firebase out',
    () async {
      await signIn();
      final pending = Completer<IdTokenResult>();
      firebase.nextUser = FakeUser('new-user')..pending = pending;
      final attempt = signIn();
      final expectation = expectLater(attempt, throwsA(isA<AuthFailure>()));
      await flush();
      pending.completeError(
        FirebaseAuthException(code: 'network-request-failed'),
      );
      await expectation;
      expect(repository.currentUser, isNull);
      expect(firebase.currentUser, isNull);
    },
  );

  for (final entry in {
    'invalid-credential': 'Incorrect email or password.',
    'wrong-password': 'Incorrect email or password.',
    'user-not-found': 'Incorrect email or password.',
    'invalid-email': 'Please enter a valid email address.',
    'user-disabled': 'This account has been disabled.',
    'network-request-failed': 'Check your connection and try again.',
    'too-many-requests': 'Too many attempts. Please try again later.',
    'unknown': 'Unable to sign in. Please try again.',
  }.entries) {
    test('safe error mapping: ${entry.key}', () async {
      firebase.failureCode = entry.key;
      await expectLater(
        signIn(),
        throwsA(
          isA<AuthFailure>().having((e) => e.message, 'message', entry.value),
        ),
      );
      expect(repository.currentUser, isNull);
    });
  }

  for (final manager in [false, true]) {
    testWidgets(
      'Firebase login routes trusted manager=$manager and logout guards root',
      (tester) async {
        firebase.nextUser = FakeUser(
          'widget-uid',
          manager ? {'role': 'manager'} : {},
        );
        await tester.pumpWidget(SmartCartApp(authRepository: repository));
        await tester.pumpAndSettle();
        expect(find.text('Temporary test role'), findsNothing);
        expect(find.text('Development demo only'), findsNothing);
        await tester.enterText(
          find.widgetWithText(TextFormField, 'Email'),
          'manager@example.test',
        );
        await tester.enterText(
          find.widgetWithText(TextFormField, 'Password'),
          'test-only',
        );
        await tester.tap(find.text('Sign In'));
        await tester.pumpAndSettle();
        expect(
          find.text(manager ? 'Manager Dashboard' : 'Customer Cart'),
          findsOneWidget,
        );
        expect(
          find.text(manager ? 'Customer Cart' : 'Manager Dashboard'),
          findsNothing,
        );
        await tester.scrollUntilVisible(find.text('Logout'), 300);
        await tester.tap(find.text('Logout'));
        await tester.pumpAndSettle();
        expect(find.text('Welcome Back'), findsOneWidget);
        expect(
          Navigator.of(tester.element(find.text('Welcome Back'))).canPop(),
          isFalse,
        );
        await tester.pumpWidget(const SizedBox.shrink());
      },
    );
  }
}
