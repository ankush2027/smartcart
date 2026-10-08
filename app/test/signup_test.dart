import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/app.dart';
import 'package:smartcart/features/auth/controllers/auth_controller.dart';
import 'package:smartcart/features/auth/data/auth_repository.dart';
import 'package:smartcart/features/auth/data/firebase_auth_repository.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';
import 'package:smartcart/features/auth/screens/login_screen.dart';
import 'package:smartcart/features/auth/screens/signup_screen.dart';
import 'package:smartcart/features/customer/data/mock_cart_repository.dart';
import 'package:smartcart/features/manager/data/mock_manager_repository.dart';

import 'firebase_auth_test.dart' show FakeFirebaseAuth, FakeUser, FakeCredential, flush;
import 'customer_live_test.dart' show LiveCart, FakePairing;

class ProfileUser extends FakeUser {
  ProfileUser() : super('new-customer');
  String? savedName;
  Completer<void>? profilePending;
  bool failProfile = false;
  @override
  Future<void> updateDisplayName(String? displayName) async {
    if (profilePending != null) await profilePending!.future;
    if (failProfile) throw FirebaseAuthException(code: 'network-request-failed');
    savedName = displayName;
  }
}

class RegistrationFirebase extends FakeFirebaseAuth {
  int registrations = 0;
  String? registeredEmail;
  @override
  Future<UserCredential> createUserWithEmailAndPassword({
    required String email, required String password,
  }) async {
    registrations++;
    registeredEmail = email;
    if (failureCode != null) {
      throw FirebaseAuthException(code: failureCode!, message: 'private SDK diagnostic');
    }
    change(nextUser);
    return FakeCredential(nextUser);
  }
}

class RegistrationRepository implements AuthRepository, CustomerRegistration {
  @override
  AuthUser? currentUser;
  int calls = 0;
  Completer<AuthUser>? pending;
  @override
  Future<AuthUser> signUp({required String fullName, required String email,
    required String password}) async {
    calls++;
    return currentUser = pending == null
        ? const AuthUser(uid: 'new-customer', role: UserRole.customer)
        : await pending!.future;
  }
  @override
  Future<AuthUser> signIn({required String email, required String password}) async =>
      currentUser = const AuthUser(uid: 'returning', role: UserRole.customer);
  @override
  Future<void> signOut() async { currentUser = null; }
}

Future<void> submit(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(const Key('submit_signup')));
  await tester.tap(find.byKey(const Key('submit_signup')));
  await tester.pump();
}

Future<void> fill(WidgetTester tester, {String confirmation = 'test-password'}) async {
  await tester.enterText(find.byKey(const Key('signup_name')), 'New Customer');
  await tester.enterText(find.byKey(const Key('signup_email')), 'new@example.test');
  await tester.enterText(find.byKey(const Key('signup_password')), 'test-password');
  await tester.enterText(find.byKey(const Key('signup_confirmation')), confirmation);
}

void main() {
  test('Firebase registration sets profile then publishes trusted customer identity', () async {
    final firebase = RegistrationFirebase();
    final user = ProfileUser()..profilePending = Completer<void>();
    firebase.nextUser = user;
    final repository = FirebaseAuthRepository(firebaseAuth: firebase);
    final controller = AuthController(repository);
    await flush();
    final signup = controller.signUp(fullName: ' New Customer ',
      email: ' new@example.test ', password: 'test-password');
    await flush();
    expect(controller.user, isNull);
    expect(controller.isBusy, isTrue);
    await controller.signUp(fullName: 'Duplicate', email: 'duplicate@example.test', password: 'test-password');
    expect(firebase.registrations, 1);
    user.profilePending!.complete();
    await signup;
    expect(firebase.registeredEmail, 'new@example.test');
    expect(user.savedName, 'New Customer');
    expect(controller.user!.uid, 'new-customer');
    expect(controller.user!.role, UserRole.customer);
    expect(controller.supportsDevelopmentRoles, isFalse);
    await controller.signOut();
    expect(controller.user, isNull);
    expect(firebase.currentUser, isNull);
    controller.dispose();
    repository.dispose();
    await firebase.events.close();
  });

  for (final code in ['email-already-in-use', 'weak-password', 'network-request-failed',
    'operation-not-allowed', 'too-many-requests']) {
    test('signup error $code stays signed out with sanitized message', () async {
      final firebase = RegistrationFirebase()..failureCode = code;
      final repository = FirebaseAuthRepository(firebaseAuth: firebase);
      final controller = AuthController(repository);
      await flush();
      await controller.signUp(fullName: 'Name', email: 'new@example.test', password: 'test-password');
      expect(controller.user, isNull);
      expect(repository.currentUser, isNull);
      expect(controller.error, isNotNull);
      expect(controller.error, isNot(contains('private SDK')));
      expect(controller.isBusy, isFalse);
      controller.dispose(); repository.dispose();
      await firebase.events.close();
    });
  }

  test('profile failure acknowledges existing account without silently retrying signup', () async {
    final firebase = RegistrationFirebase()..nextUser = (ProfileUser()..failProfile = true);
    final repository = FirebaseAuthRepository(firebaseAuth: firebase);
    await expectLater(repository.signUp(fullName: 'Name', email: 'new@example.test', password: 'test-password'),
      throwsA(isA<AuthFailure>().having((e) => e.message, 'message', contains('account was created'))));
    expect(firebase.registrations, 1);
    expect(firebase.currentUser, isNull);
    expect(repository.currentUser, isNull);
    repository.dispose(); await firebase.events.close();
  });

  testWidgets('required fields, email, password mismatch and visibility', (tester) async {
    final repository = RegistrationRepository();
    final auth = AuthController(repository);
    await tester.pumpWidget(MaterialApp(home: SignupScreen(controller: auth, onLogin: () {})));
    await submit(tester);
    expect(repository.calls, 0);
    expect(find.text('Full name is required.'), findsOneWidget);
    expect(find.text('Please enter a valid email address.'), findsOneWidget);
    expect(find.text('Use at least 6 characters.'), findsOneWidget);
    await fill(tester, confirmation: 'different');
    await submit(tester);
    expect(find.text('Passwords do not match.'), findsOneWidget);
    expect(repository.calls, 0);
    await tester.ensureVisible(find.byTooltip('Show passwords'));
    await tester.tap(find.byTooltip('Show passwords'));
    await tester.pump();
    expect(tester.widget<TextFormField>(find.byKey(const Key('signup_password'))).controller!.text, 'test-password');
    expect(find.byTooltip('Hide passwords'), findsOneWidget);
    await tester.pumpWidget(const SizedBox()); auth.dispose();
  });

  testWidgets('login/signup navigation and submission loading', (tester) async {
    final repository = RegistrationRepository()..pending = Completer<AuthUser>();
    final auth = AuthController(repository);
    await tester.pumpWidget(MaterialApp(home: LoginScreen(controller: auth)));
    await tester.ensureVisible(find.byKey(const Key('open_signup')));
    await tester.tap(find.byKey(const Key('open_signup')));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Already have an account? Log In'));
    await tester.tap(find.text('Already have an account? Log In'));
    await tester.pumpAndSettle();
    expect(find.text('Welcome Back'), findsOneWidget);
    await tester.ensureVisible(find.byKey(const Key('open_signup')));
    await tester.tap(find.byKey(const Key('open_signup')));
    await tester.pumpAndSettle();
    await fill(tester); await submit(tester);
    expect(repository.calls, 1);
    expect(tester.widget<FilledButton>(find.byKey(const Key('submit_signup'))).onPressed, isNull);
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    repository.pending!.complete(const AuthUser(uid: 'new-customer', role: UserRole.customer));
    await tester.pumpAndSettle();
    await tester.pumpWidget(const SizedBox()); auth.dispose();
  });

  testWidgets('signup reaches cart entry without automatic pairing and logout clears listener', (tester) async {
    final auth = RegistrationRepository();
    final cart = LiveCart();
    final pairing = FakePairing();
    await tester.pumpWidget(SmartCartApp(authRepository: auth, cartRepository: cart,
      pairingService: pairing, managerRepository: MockManagerRepository()));
    await tester.ensureVisible(find.byKey(const Key('open_signup')));
    await tester.tap(find.byKey(const Key('open_signup')));
    await tester.pumpAndSettle();
    await fill(tester); await submit(tester);
    await tester.pump();
    cart.events.add(null);
    await tester.pumpAndSettle();
    expect(find.text('Scan Cart QR Code'), findsOneWidget);
    expect(find.text('Enter Cart ID'), findsOneWidget);
    expect(pairing.requests, isEmpty);
    await tester.ensureVisible(find.text('Logout'));
    await tester.tap(find.text('Logout'));
    await tester.pumpAndSettle();
    expect(find.text('Welcome Back'), findsOneWidget);
    expect(cart.events.hasListener, isFalse);
    expect(pairing.requests, isEmpty);
    await tester.pumpWidget(const SizedBox()); await cart.dispose();
  });

  testWidgets('persisted manager still routes to manager without registration', (tester) async {
    final firebase = RegistrationFirebase()
      ..currentUser = FakeUser('manager', {'role': 'manager'});
    final repository = FirebaseAuthRepository(firebaseAuth: firebase);
    await tester.pumpWidget(SmartCartApp(authRepository: repository,
      cartRepository: MockCartRepository(), managerRepository: MockManagerRepository()));
    await tester.pumpAndSettle();
    expect(find.text('Manager Dashboard'), findsOneWidget);
    expect(find.text('Create Account'), findsNothing);
    expect(find.text('Customer Cart'), findsNothing);
    expect(firebase.registrations, 0);
    await tester.pumpWidget(const SizedBox());
    repository.dispose(); await firebase.events.close();
  });
}
