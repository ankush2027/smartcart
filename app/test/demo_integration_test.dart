import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/features/auth/controllers/auth_controller.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';
import 'package:smartcart/features/checkout/controllers/checkout_controller.dart';
import 'package:smartcart/features/checkout/models/checkout_models.dart';
import 'package:smartcart/features/customer/data/firebase_cart_repository.dart';
import 'package:smartcart/features/customer/models/customer_cart.dart';
import 'package:smartcart/features/customer/screens/customer_screen.dart';

import 'checkout_test.dart' show FakeCheckoutRepository, frozenCheckout,
    orderReadyAttempt, completedAttempt;
import 'customer_live_test.dart' show LiveCart, SwitchingAuth;
import 'rtdb_mapping_test.dart' show sessionFixture;
import 'rtdb_repository_test.dart' show FakeReader, LiveAuth, flush;

CustomerCart active(String id) => CustomerCart(
  displayId: id,
  cartId: 'CART-001',
  status: CartStatus.active,
  items: const [],
  subtotalPaise: 1000,
  totalPaise: 1000,
);

Future<void> tap(WidgetTester tester, String key) async {
  final target = find.byKey(Key(key));
  await tester.ensureVisible(target);
  await tester.tap(target);
  await tester.pumpAndSettle();
}

void main() {
  test('backend FAILED order never becomes payment-ready', () async {
    final repository = FakeCheckoutRepository()
      ..beginCheckoutResponse = (() async => frozenCheckout())
      ..startPaymentResponse = (() async => PaymentAttempt.fromJson({
        'payment_attempt_id': 'c4689221b0544a32b8dba2f7cf146a13',
        'checkout_id': 42,
        'amount_paise': 25000,
        'currency': 'INR',
        'state': 'FAILED',
        'error_code': 'provider_rejected',
      }));
    final controller = CheckoutController(repository);
    await controller.beginCheckout(sessionId: 1, idempotencyKey: 'checkout');
    await controller.startPayment(sessionId: 1, checkoutId: 42, idempotencyKey: 'payment');
    expect(controller.paymentAttempt!.state, PaymentState.creationFailed);
    expect(controller.phase, CheckoutControllerPhase.frozen);
    expect(controller.error, isNotNull);
    await controller.simulateDemoPayment(sessionId: 1, checkoutId: 42);
    expect(repository.simulateDemoPaymentCalls, 0);
    expect(controller.paymentAttempt!.isCompleted, isFalse);
    controller.dispose();
  });

  test('opaque backend payment identity survives parsing', () {
    final attempt = PaymentAttempt.fromJson({
      'payment_attempt_id': 'c4689221b0544a32b8dba2f7cf146a13',
      'checkout_id': 3,
      'amount_paise': 1000,
      'currency': 'INR',
      'state': 'COMPLETED',
    });
    expect(attempt.paymentAttemptId, 'c4689221b0544a32b8dba2f7cf146a13');
    expect(attempt.isCompleted, isTrue);
  });

  test('orphan remains visible until backend repair; completed history is excluded', () async {
    final auth = LiveAuth(const AuthUser(uid: 'customer-uid', role: UserRole.customer));
    final reader = FakeReader();
    final repo = FirebaseCartRepository(auth: auth, reader: reader);
    final carts = <CustomerCart?>[];
    final subscription = repo.watchCurrentCart().listen(carts.add);
    await flush();
    reader.data.add({'1': sessionFixture()});
    await flush();
    expect(carts.last!.displayId, '1'); // Client cannot infer missing SQLite row.
    reader.data.add({'2': sessionFixture(completed: true)}); // Backend repair.
    await flush();
    expect(carts.last, isNull);
    reader.data.add({'2': sessionFixture(completed: true), '3': sessionFixture()});
    await flush();
    expect(carts.last!.displayId, '3');
    expect(reader.paths, ['sessions:customer_uid=customer-uid']);
    await subscription.cancel();
    await repo.dispose();
    await reader.close();
    await auth.changes.close();
  });

  for (final enabled in [false, true]) {
    testWidgets('live Firebase cart demo payment opt-in=$enabled and completion race', (tester) async {
      final authSource = SwitchingAuth();
      final auth = AuthController(authSource);
      final cart = LiveCart();
      final response = Completer<PaymentAttempt>();
      final checkout = FakeCheckoutRepository()
        ..beginCheckoutResponse = (() async => frozenCheckout())
        ..startPaymentResponse = (() async => orderReadyAttempt())
        ..simulateDemoPaymentResponse = (() => response.future);
      await tester.pumpWidget(MaterialApp(home: CustomerScreen(
        auth: auth,
        repository: cart,
        checkoutRepository: checkout,
        demoPaymentEnabled: enabled,
      )));
      await tester.pump();
      cart.events.add(active('1'));
      await tester.pumpAndSettle();
      await tap(tester, 'checkout_button');
      await tap(tester, 'start_payment_button');
      expect(find.byKey(const Key('demo_payment_button')), enabled ? findsOneWidget : findsNothing);
      expect(find.textContaining('Sample cart and prices'), findsNothing);
      if (enabled) {
        await tester.ensureVisible(find.byKey(const Key('demo_payment_button')));
        await tester.tap(find.byKey(const Key('demo_payment_button')));
        await tester.pump();
        cart.events.add(null); // Completion projection beats backend response.
        await tester.pump();
        expect(find.text('Payment Successful'), findsNothing);
        response.complete(completedAttempt());
        await tester.pumpAndSettle();
        expect(find.text('Payment Successful'), findsOneWidget);
        cart.events.add(active('2')); // A new session must clear the receipt.
        await tester.pumpAndSettle();
        expect(find.text('Payment Successful'), findsNothing);
        expect(find.byKey(const Key('checkout_button')), findsOneWidget);
      }
      await tester.pumpWidget(const SizedBox());
      auth.dispose();
      await cart.dispose();
      await authSource.events.close();
    });
  }
}
