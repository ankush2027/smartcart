import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/features/auth/controllers/auth_controller.dart';
import 'package:smartcart/features/checkout/controllers/checkout_controller.dart';
import 'package:smartcart/features/checkout/data/checkout_repository.dart';
import 'package:smartcart/features/customer/screens/customer_screen.dart';

import 'checkout_test.dart';
import 'customer_live_test.dart' show LiveCart, SwitchingAuth;
import 'demo_integration_test.dart' show active, tap;

Map<String, dynamic> frozenState() => {
  'state': 'CHECKOUT_FROZEN',
  'latest_checkout': {
    'session_id': 1,
    'cart_id': 'CART-001',
    'checkout_id': 42,
    'state': 'FROZEN',
    'final_amount': 250,
  },
  'items': [
    {'name': 'Lays', 'quantity': 2, 'total_paise': 25000},
  ],
};

void main() {
  test(
    'ambiguous checkout retry retains its original idempotency key',
    () async {
      final repo = FakeCheckoutRepository()
        ..beginCheckoutResponse = (() async =>
            throw const CheckoutFailure('timeout'));
      final controller = CheckoutController(repo);
      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'original');
      repo.beginCheckoutResponse = () async => frozenCheckout();
      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'new-tap');
      expect(repo.checkoutKeys, ['original', 'original']);
      expect(controller.phase, CheckoutControllerPhase.frozen);
      controller.dispose();
    },
  );

  test(
    'lost cancellation response returns to shopping only after confirmed read',
    () async {
      final repo = FakeCheckoutRepository()
        ..beginCheckoutResponse = (() async => frozenCheckout())
        ..cancelCheckoutResponse = (() async =>
            throw const CheckoutFailure('timeout'));
      final controller = CheckoutController(repo);
      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'freeze');
      await controller.cancelCheckout(sessionId: 1, checkoutId: 42);
      expect(controller.phase, CheckoutControllerPhase.shopping);
      expect(controller.checkoutResult, isNull);
      controller.dispose();
    },
  );

  test(
    'restart restores frozen checkout and authoritative receipt items',
    () async {
      final repo = FakeCheckoutRepository()
        ..readCheckoutStateResponse = (() async => frozenState())
        ..paymentStatusResponse = (() async =>
            throw const CheckoutFailure('payment_not_found'));
      final controller = CheckoutController(repo);
      await controller.recover(1);
      expect(controller.phase, CheckoutControllerPhase.frozen);
      expect(controller.items.single['name'], 'Lays');
      expect(repo.beginCheckoutCalls, 0);
      controller.dispose();
    },
  );

  test(
    'lost checkout response resolves existing freeze without another POST',
    () async {
      final repo = FakeCheckoutRepository()
        ..beginCheckoutResponse = (() async =>
            throw const CheckoutFailure('timeout'))
        ..readCheckoutStateResponse = (() async => frozenState())
        ..paymentStatusResponse = (() async =>
            throw const CheckoutFailure('payment_not_found'));
      final controller = CheckoutController(repo);
      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'one');
      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'two');
      expect(controller.phase, CheckoutControllerPhase.frozen);
      expect(repo.beginCheckoutCalls, 1);
      controller.dispose();
    },
  );

  test(
    'lost payment response restores reserved order and blocks duplicate charge',
    () async {
      final repo = FakeCheckoutRepository()
        ..beginCheckoutResponse = (() async => frozenCheckout())
        ..startPaymentResponse = (() async =>
            throw const CheckoutFailure('timeout'))
        ..readCheckoutStateResponse = (() async => frozenState())
        ..paymentStatusResponse = (() async => orderReadyAttempt());
      final controller = CheckoutController(repo);
      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'one');
      await controller.startPayment(
        sessionId: 1,
        checkoutId: 42,
        idempotencyKey: 'pay',
      );
      await controller.startPayment(
        sessionId: 1,
        checkoutId: 42,
        idempotencyKey: 'pay2',
      );
      expect(controller.phase, CheckoutControllerPhase.paymentReady);
      expect(repo.startPaymentCalls, 1);
      controller.dispose();
    },
  );

  test(
    'offline recovery blocks checkout until authoritative retry succeeds',
    () async {
      final repo = FakeCheckoutRepository()
        ..readCheckoutStateResponse = (() async =>
            throw const CheckoutFailure('network'));
      final controller = CheckoutController(repo);
      await controller.recover(1);
      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'unsafe');
      expect(repo.beginCheckoutCalls, 0);
      expect(controller.phase, CheckoutControllerPhase.recoveryRequired);
      repo.readCheckoutStateResponse = () async => {'state': 'SHOPPING'};
      await controller.recover(1);
      expect(controller.phase, CheckoutControllerPhase.shopping);
      controller.dispose();
    },
  );

  test('reset fences stale recovery from previous customer', () async {
    final pending = Completer<Map<String, dynamic>>();
    final repo = FakeCheckoutRepository()
      ..readCheckoutStateResponse = (() => pending.future);
    final controller = CheckoutController(repo);
    final recovery = controller.recover(1);
    controller.reset();
    pending.complete(frozenState());
    await recovery;
    expect(controller.phase, CheckoutControllerPhase.shopping);
    expect(controller.checkoutResult, isNull);
    controller.dispose();
  });

  testWidgets('restart shows Pay Now and survives loss of Firebase session', (
    tester,
  ) async {
    final authSource = SwitchingAuth();
    final auth = AuthController(authSource);
    final cart = LiveCart();
    final repo = FakeCheckoutRepository()
      ..readCheckoutStateResponse = (() async => frozenState())
      ..paymentStatusResponse = (() async =>
          throw const CheckoutFailure('payment_not_found'))
      ..startPaymentResponse = (() async => orderReadyAttempt())
      ..simulateDemoPaymentResponse = (() async => completedAttempt());
    await tester.pumpWidget(
      MaterialApp(
        home: CustomerScreen(
          auth: auth,
          repository: cart,
          checkoutRepository: repo,
          demoPaymentEnabled: true,
        ),
      ),
    );
    await tester.pump();
    cart.events.add(active('1'));
    await tester.pumpAndSettle();
    expect(find.text('Lays'), findsOneWidget);
    expect(find.text('Pay Now'), findsOneWidget);
    expect(repo.beginCheckoutCalls, 0);
    cart.events.add(null);
    await tester.pumpAndSettle();
    await tap(tester, 'start_payment_button');
    await tap(tester, 'demo_payment_button');
    expect(find.text('Payment Successful'), findsOneWidget);
    expect(find.text('Lays'), findsOneWidget);
    expect(repo.simulateDemoPaymentCalls, 1);
    await tester.pumpWidget(const SizedBox());
    auth.dispose();
    await cart.dispose();
    await authSource.events.close();
  });
}
