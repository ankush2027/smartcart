import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/app.dart';
import 'package:smartcart/core/data/repository_failure.dart';
import 'package:smartcart/features/auth/data/in_memory_auth_repository.dart';
import 'package:smartcart/features/auth/data/auth_repository.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';
import 'package:smartcart/features/customer/controllers/cart_controller.dart';
import 'package:smartcart/features/customer/data/cart_repository.dart';
import 'package:smartcart/features/customer/models/customer_cart.dart';
import 'package:smartcart/features/customer/services/cart_pairing_service.dart';

class SwitchingAuth implements AuthRepository, AuthStateSource {
  @override
  AuthUser? currentUser = const AuthUser(uid: 'a', role: UserRole.customer);
  final events = StreamController<AuthUser?>.broadcast(sync: true);
  @override
  Stream<AuthUser?> get authStateChanges => events.stream;
  void switchTo(String uid) {
    currentUser = AuthUser(uid: uid, role: UserRole.customer);
    events.add(currentUser);
  }

  @override
  Future<AuthUser> signIn({
    required String email,
    required String password,
  }) async => currentUser!;
  @override
  Future<void> signOut() async {
    currentUser = null;
    events.add(null);
  }
}

class LiveCart implements RealtimeCartRepository {
  final events = StreamController<CustomerCart?>.broadcast(sync: true);
  int starts = 0;
  @override
  bool get isDemo => false;
  @override
  Stream<CustomerCart?> watchCurrentCart() {
    starts++;
    return events.stream;
  }

  @override
  Future<CustomerCart?> loadCurrentCart() => throw StateError('must listen');
  @override
  Future<void> dispose() => events.close();
}

class FakePairing implements CartPairingService {
  final requests = <String>[];
  Completer<void> pending = Completer<void>();
  @override
  Future<void> pair(String cartId) {
    requests.add(cartId);
    return pending.future;
  }
}

CustomerCart liveCart({int total = 1025, bool empty = false}) => CustomerCart(
  displayId: 'session-42',
  cartId: 'CART-001',
  status: CartStatus.active,
  items: empty
      ? []
      : [
          const CartItem(
            product: CartProduct(name: 'Live item'),
            quantity: 1,
            unitPricePaise: 1025,
            lineTotalPaise: 1025,
          ),
        ],
  subtotalPaise: empty ? 0 : 1025,
  totalPaise: total,
);
void main() {
  test(
    'live discovery, duplicate pairing, waiting and updates use one listener',
    () async {
      final repo = LiveCart();
      final api = FakePairing();
      final controller = CartController(repo, pairing: api);
      await controller.load();
      repo.events.add(null);
      expect(controller.state, CartLoadState.noSession);
      final pairing = controller.pair('CART-001');
      expect(controller.state, CartLoadState.pairing);
      await controller.pair('CART-002');
      expect(api.requests, ['CART-001']);
      api.pending.complete();
      await pairing;
      expect(controller.state, CartLoadState.connecting);
      expect(controller.cart, isNull);
      repo.events.add(liveCart());
      expect(controller.state, CartLoadState.ready);
      repo.events.add(liveCart(total: 2000));
      expect(controller.cart!.totalPaise, 2000);
      expect(repo.starts, 1);
      controller.dispose();
      expect(repo.events.hasListener, isFalse);
      await repo.dispose();
    },
  );
  test(
    'live session may arrive before HTTP response without being overwritten',
    () async {
      final repo = LiveCart();
      final api = FakePairing();
      final controller = CartController(repo, pairing: api);
      await controller.load();
      repo.events.add(null);
      final request = controller.pair('CART-001');
      repo.events.add(liveCart(empty: true, total: 0));
      api.pending.complete();
      await request;
      expect(controller.state, CartLoadState.ready);
      expect(controller.cart!.items, isEmpty);
      controller.dispose();
      await repo.dispose();
    },
  );
  testWidgets('sync timeout is bounded and retry only restarts discovery', (
    tester,
  ) async {
    final repo = LiveCart();
    final api = FakePairing();
    final controller = CartController(
      repo,
      pairing: api,
      syncWindow: const Duration(seconds: 2),
    );
    await tester.runAsync(controller.load);
    repo.events.add(null);
    final attempt = controller.pair('CART-001');
    api.pending.complete();
    await tester.runAsync(() => attempt);
    await tester.pump(const Duration(seconds: 3));
    expect(controller.state, CartLoadState.syncError);
    expect(controller.cart, isNull);
    await tester.runAsync(controller.load);
    expect(api.requests.length, 1);
    expect(repo.starts, 2);
    repo.events.add(liveCart());
    expect(controller.state, CartLoadState.ready);
    controller.dispose();
    await tester.runAsync(repo.dispose);
  });
  test(
    'API failure is retryable; RTDB permission failure clears data',
    () async {
      final repo = LiveCart();
      final api = FakePairing();
      final controller = CartController(repo, pairing: api);
      await controller.load();
      repo.events.add(null);
      final attempt = controller.pair('CART-001');
      api.pending.completeError(const PairingFailure('cart_already_assigned'));
      await attempt;
      expect(controller.state, CartLoadState.noSession);
      expect(controller.error, contains('another customer'));
      api.pending = Completer<void>();
      final retry = controller.pair('CART-001');
      api.pending.complete();
      await retry;
      repo.events.addError(
        const RepositoryFailure(RepositoryFailureCode.permissionDenied),
      );
      expect(controller.state, CartLoadState.error);
      expect(controller.cart, isNull);
      expect(controller.error, contains('access'));
      await controller.load();
      repo.events.add(liveCart());
      expect(controller.state, CartLoadState.ready);
      controller.dispose();
      await repo.dispose();
    },
  );
  test(
    'disposed pairing completion and late live data cannot restore state',
    () async {
      final repo = LiveCart();
      final api = FakePairing();
      final controller = CartController(repo, pairing: api);
      await controller.load();
      repo.events.add(null);
      final attempt = controller.pair('CART-001');
      controller.dispose();
      api.pending.complete();
      await attempt;
      repo.events.add(liveCart());
      expect(controller.cart, isNull);
      expect(repo.events.hasListener, isFalse);
      await repo.dispose();
    },
  );
  testWidgets(
    'customer pairing UI connects then renders real empty/live cart',
    (tester) async {
      final repo = LiveCart();
      final api = FakePairing();
      final auth = InMemoryAuthRepository(delay: Duration.zero);
      await tester.runAsync(
        () => auth.signIn(email: 'demo@example.test', password: 'demo'),
      );
      await tester.pumpWidget(
        SmartCartApp(
          authRepository: auth,
          cartRepository: repo,
          pairingService: api,
        ),
      );
      await tester.pump();
      repo.events.add(null);
      await tester.pump();
      expect(find.text('Enter Cart ID'), findsOneWidget);
      await tester.tap(find.text('Connect Cart'));
      await tester.pump();
      expect(find.text('Enter a Cart ID.'), findsOneWidget);
      await tester.enterText(find.byType(TextFormField), 'CART-001');
      await tester.tap(find.text('Connect Cart'));
      await tester.pump();
      expect(
        tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
        isNull,
      );
      api.pending.complete();
      await tester.pump();
      await tester.pump();
      expect(find.text('Connecting to your live cart...'), findsOneWidget);
      repo.events.add(liveCart(empty: true, total: 0));
      await tester.pump();
      expect(find.text('Your cart is empty.'), findsOneWidget);
      expect(find.text('Enter Cart ID'), findsNothing);
      repo.events.add(liveCart());
      await tester.pump();
      expect(find.text('Live item'), findsOneWidget);
      expect(find.text('Cart ID: CART-001'), findsOneWidget);
      await tester.scrollUntilVisible(find.text('Logout'), 300);
      await tester.tap(find.text('Logout'));
      await tester.pumpAndSettle();
      expect(find.text('Welcome Back'), findsOneWidget);
      expect(repo.events.hasListener, isFalse);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.runAsync(repo.dispose);
    },
  );
  testWidgets('logout while pairing discards input and pending response', (
    tester,
  ) async {
    final repo = LiveCart();
    final api = FakePairing();
    final auth = InMemoryAuthRepository(delay: Duration.zero);
    await tester.runAsync(
      () => auth.signIn(email: 'demo@example.test', password: 'demo'),
    );
    await tester.pumpWidget(
      SmartCartApp(
        authRepository: auth,
        cartRepository: repo,
        pairingService: api,
      ),
    );
    await tester.pump();
    repo.events.add(null);
    await tester.pump();
    await tester.enterText(find.byType(TextFormField), 'OLD-CART');
    await tester.tap(find.text('Connect Cart'));
    await tester.pump();
    await tester.tap(find.text('Logout'));
    await tester.pumpAndSettle();
    api.pending.complete();
    await tester.pumpAndSettle();
    expect(find.text('Welcome Back'), findsOneWidget);
    expect(find.text('OLD-CART'), findsNothing);
    expect(repo.events.hasListener, isFalse);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.runAsync(repo.dispose);
  });
  testWidgets(
    'customer switch clears live cart, pairing input and late response',
    (tester) async {
      final auth = SwitchingAuth();
      final repo = LiveCart();
      final api = FakePairing();
      await tester.pumpWidget(
        SmartCartApp(
          authRepository: auth,
          cartRepository: repo,
          pairingService: api,
        ),
      );
      await tester.pump();
      repo.events.add(liveCart());
      await tester.pump();
      expect(find.text('Live item'), findsOneWidget);
      auth.switchTo('b');
      await tester.pump();
      expect(find.text('Live item'), findsNothing);
      repo.events.add(null);
      await tester.pump();
      await tester.enterText(find.byType(TextFormField), 'B-CART');
      await tester.tap(find.text('Connect Cart'));
      await tester.pump();
      auth.switchTo('c');
      await tester.pump();
      repo.events.add(null);
      await tester.pump();
      api.pending.complete();
      await tester.pump();
      expect(find.text('B-CART'), findsNothing);
      expect(find.text('Connect Cart'), findsOneWidget);
      expect(find.text('Connecting to your live cart...'), findsNothing);
      expect(
        tester
            .widget<TextFormField>(find.byType(TextFormField))
            .controller!
            .text,
        isEmpty,
      );
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.runAsync(repo.dispose);
      await tester.runAsync(auth.events.close);
    },
  );
}
