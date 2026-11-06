import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/app.dart';
import 'package:smartcart/features/auth/data/in_memory_auth_repository.dart';
import 'package:smartcart/features/customer/controllers/cart_controller.dart';
import 'package:smartcart/features/customer/data/cart_repository.dart';
import 'package:smartcart/features/customer/data/mock_cart_repository.dart';
import 'package:smartcart/features/customer/models/customer_cart.dart';

class TestCartRepository implements CartRepository {
  int calls = 0;
  Future<CustomerCart?> Function() response = () async => null;
  @override
  bool get isDemo => false;
  @override
  Future<CustomerCart?> loadCurrentCart() {
    calls++;
    return response();
  }
}

CustomerCart sampleCart() => CustomerCart(
  displayId: 'Test cart',
  status: CartStatus.active,
  items: [
    const CartItem(
      product: CartProduct(name: 'Sample snack'),
      quantity: 2,
      unitPricePaise: 1025,
      lineTotalPaise: 2050,
    ),
  ],
  subtotalPaise: 2050,
  // Intentionally different: the UI must honor the repository's total.
  totalPaise: 1999,
);

Future<void> showCustomer(
  WidgetTester tester,
  CartRepository repository,
) async {
  final auth = InMemoryAuthRepository(delay: Duration.zero);
  await tester.runAsync(
    () => auth.signIn(email: 'demo@example.test', password: 'demo'),
  );
  await tester.pumpWidget(
    SmartCartApp(authRepository: auth, cartRepository: repository),
  );
  await tester.pump();
}

void main() {
  test('money and counts use integer paise and immutable item snapshots', () {
    final cart = sampleCart();
    expect(formatRupees(0), '₹0.00');
    expect(formatRupees(5), '₹0.05');
    expect(formatRupees(1025), '₹10.25');
    expect(cart.itemCount, 2);
    expect(cart.totalPaise, 1999);
    expect(() => cart.items.clear(), throwsUnsupportedError);
  });

  test(
    'mock fixtures support active, empty, no-session, and failure states',
    () async {
      final active = await MockCartRepository(delay: Duration.zero)
          .loadCurrentCart();
      expect(active!.items.length, 6);
      expect(active.itemCount, 8);
      expect(active.totalPaise, 13000);
      expect(active.subtotalPaise, 13000);
      final empty = await MockCartRepository(
        scenario: MockCartScenario.empty,
        delay: Duration.zero,
      ).loadCurrentCart();
      expect(empty!.items, isEmpty);
      expect(empty.totalPaise, 0);
      expect(
        await MockCartRepository(
          scenario: MockCartScenario.noSession,
          delay: Duration.zero,
        ).loadCurrentCart(),
        isNull,
      );
      await expectLater(
        MockCartRepository(
          scenario: MockCartScenario.failure,
          delay: Duration.zero,
        ).loadCurrentCart(),
        throwsStateError,
      );
    },
  );

  test('controller handles loading, duplicate requests, errors, retry, and disposal', () async {
    final repository = TestCartRepository();
    final pending = Completer<CustomerCart?>();
    repository.response = () => pending.future;
    final controller = CartController(repository);
    final load = controller.load();
    expect(controller.state, CartLoadState.loading);
    await controller.load();
    expect(repository.calls, 1);
    pending.completeError(StateError('internal detail'));
    await load;
    expect(controller.state, CartLoadState.error);
    expect(controller.cart, isNull);
    repository.response = () async => sampleCart();
    await controller.load();
    expect(repository.calls, 2);
    expect(controller.state, CartLoadState.ready);
    expect(controller.cart!.itemCount, 2);
    repository.response = () async => null;
    await controller.load();
    expect(controller.state, CartLoadState.noSession);
    final last = Completer<CustomerCart?>();
    repository.response = () => last.future;
    final finalLoad = controller.load();
    controller.dispose();
    last.complete(sampleCart());
    await finalLoad;
    expect(controller.cart, isNull);
  });

  testWidgets(
    'active cart renders quantities, prices, and supplied bill totals',
    (tester) async {
      final repository = TestCartRepository()
        ..response = () async => sampleCart();
      await showCustomer(tester, repository);
      await tester.pumpAndSettle();
      expect(find.text('Customer Cart'), findsOneWidget);
      expect(find.text('Sample snack'), findsOneWidget);
      expect(find.text('Qty: 2 · ₹10.25 each'), findsOneWidget);
      expect(find.text('Line total: ₹20.50'), findsOneWidget);
      expect(find.text('Item count: 2'), findsOneWidget);
      await tester.scrollUntilVisible(find.text('Total: ₹19.99'), 200);
      expect(find.text('Items: 2'), findsOneWidget);
      expect(find.text('Subtotal: ₹20.50'), findsOneWidget);
      expect(find.text('Total: ₹19.99'), findsOneWidget);
    },
  );

  testWidgets('empty cart is a valid zero bill', (tester) async {
    await showCustomer(
      tester,
      MockCartRepository(
        scenario: MockCartScenario.empty,
        delay: Duration.zero,
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('Your cart is empty.'), findsOneWidget);
    expect(
      find.text('Products added to your SmartCart will appear here.'),
      findsOneWidget,
    );
    await tester.scrollUntilVisible(find.text('Total: ₹0.00'), 200);
    expect(find.text('Items: 0'), findsOneWidget);
    expect(find.text('Total: ₹0.00'), findsOneWidget);
    expect(find.text('Retry'), findsNothing);
  });

  testWidgets('no active session has an explanatory state without a bill', (
    tester,
  ) async {
    await showCustomer(
      tester,
      MockCartRepository(
        scenario: MockCartScenario.noSession,
        delay: Duration.zero,
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('No active cart'), findsOneWidget);
    expect(
      find.text('There is currently no active SmartCart session.'),
      findsOneWidget,
    );
    expect(find.text('Current Bill'), findsNothing);
  });

  testWidgets('loading, safe error, and retry call the repository', (
    tester,
  ) async {
    final pending = Completer<CustomerCart?>();
    final repository = TestCartRepository()..response = () => pending.future;
    await showCustomer(tester, repository);
    expect(find.text('Loading your cart...'), findsOneWidget);
    pending.completeError(StateError('secret implementation detail'));
    await tester.pumpAndSettle();
    expect(
      find.text('Unable to load your cart. Please try again.'),
      findsOneWidget,
    );
    expect(find.textContaining('secret implementation detail'), findsNothing);
    repository.response = () async => sampleCart();
    await tester.tap(find.text('Retry'));
    await tester.pumpAndSettle();
    expect(repository.calls, 2);
    expect(find.text('Sample snack'), findsOneWidget);
  });

  testWidgets(
    'logout during a pending cart load safely discards the customer screen',
    (tester) async {
      final pending = Completer<CustomerCart?>();
      final repository = TestCartRepository()..response = () => pending.future;
      await showCustomer(tester, repository);
      await tester.tap(find.text('Logout'));
      await tester.pumpAndSettle();
      expect(find.text('Welcome Back'), findsOneWidget);
      pending.complete(sampleCart());
      await tester.pumpAndSettle();
      expect(find.text('Customer Cart'), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('customer cart supports small screens and large text', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 480);
    tester.view.devicePixelRatio = 1;
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    await showCustomer(tester, MockCartRepository(delay: Duration.zero));
    await tester.pumpAndSettle();
    expect(
      find.textContaining('No live SmartCart connection.'),
      findsOneWidget,
    );
    await tester.scrollUntilVisible(find.text('Total: ₹130.00'), 200);
    await tester.ensureVisible(find.text('Total: ₹130.00'));
    await tester.pumpAndSettle();
    expect(find.text('Total: ₹130.00').hitTestable(), findsOneWidget);
    await tester.scrollUntilVisible(find.text('Logout'), 200);
    await tester.pumpAndSettle();
    expect(find.text('Logout').hitTestable(), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
