import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:smartcart/features/auth/controllers/auth_controller.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';
import 'package:smartcart/features/customer/screens/customer_screen.dart';
import 'package:smartcart/features/customer/services/cart_pairing_service.dart';
import 'package:smartcart/features/customer/services/cart_qr.dart';
import 'package:smartcart/features/customer/widgets/cart_qr_scanner.dart';

import 'customer_live_test.dart' show LiveCart, FakePairing, liveCart;
import 'pairing_service_test.dart' show TestTokens, pairingFailure;
import 'signup_test.dart' show RegistrationRepository;

void main() {
  for (final id in ['CART-001', 'CART-A12', 'CART-${'A' * 32}']) {
    test('plain cart label accepted: $id', () => expect(parseCartQr(id), id));
  }
  for (final payload in <String?>[null, '', 'CART-', 'cart-001', ' CART-001', 'CART-001 ',
    'CART-001\n', 'CART-001\r', 'CART-001\u0000', 'CART-${'A' * 33}',
    'https://example.test/CART-001', 'smartcart://CART-001',
    '{"cart_id":"CART-001"}', 'CART-001?token=secret', 'CART-001/session/42']) {
    test('invalid QR rejected: ${payload?.length}', () {
      expect(() => parseCartQr(payload), throwsFormatException);
    });
  }
  test('scan gate rejects invalid input and consumes only one valid result', () {
    final gate = CartScanGate();
    expect(() => gate.accept('website'), throwsFormatException);
    expect(gate.accept('CART-001'), 'CART-001');
    expect(gate.accept('CART-001'), isNull);
    expect(gate.accept('CART-002'), isNull);
  });

  testWidgets('invalid QR shows safe feedback, then valid QR is accepted once', (tester) async {
    late ValueChanged<String?> detect;
    final received = <String>[];
    var manual = false;
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: CartQrScanner(
      onCart: received.add, onManual: () => manual = true,
      cameraBuilder: (callback) { detect = callback; return const SizedBox(); },
    ))));
    detect('https://unrelated.test');
    await tester.pump();
    expect(find.textContaining('not a SmartCart'), findsOneWidget);
    expect(received, isEmpty);
    detect('CART-001'); detect('CART-001'); detect('CART-002');
    expect(received, ['CART-001']);
    await tester.tap(find.text('Enter Cart ID instead'));
    expect(manual, isTrue);
  });

  testWidgets('scanner closes before pairing; duplicate callbacks cannot create requests', (tester) async {
    final repo = RegistrationRepository()
      ..currentUser = const AuthUser(uid: 'customer-a', role: UserRole.customer);
    final auth = AuthController(repo);
    final carts = LiveCart();
    final pairing = FakePairing();
    late ValueChanged<String?> detect;
    await tester.pumpWidget(MaterialApp(home: CustomerScreen(
      auth: auth, repository: carts, pairing: pairing,
      cameraBuilder: (callback) { detect = callback; return const Text('Camera fixture'); },
    )));
    await tester.pump(); carts.events.add(null); await tester.pump();
    expect(pairing.requests, isEmpty);
    await tester.tap(find.byKey(const Key('scan_cart'))); await tester.pump();
    detect('CART-001'); detect('CART-001');
    await tester.pump();
    expect(pairing.requests, ['CART-001']);
    expect(find.text('Camera fixture'), findsNothing);
    pairing.pending.complete();
    await tester.pump();
    carts.events.add(liveCart()); await tester.pumpAndSettle();
    expect(find.text('Active Cart'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    auth.dispose(); await carts.dispose();
  });

  testWidgets('camera fallback preserves manual entry and occupied-cart error', (tester) async {
    final repo = RegistrationRepository()
      ..currentUser = const AuthUser(uid: 'customer-a', role: UserRole.customer);
    final auth = AuthController(repo);
    final carts = LiveCart();
    final pairing = FakePairing();
    await tester.pumpWidget(MaterialApp(home: CustomerScreen(
      auth: auth, repository: carts, pairing: pairing,
      cameraBuilder: (_) => const Text('Camera unavailable'),
    )));
    await tester.pump(); carts.events.add(null); await tester.pump();
    await tester.tap(find.byKey(const Key('scan_cart'))); await tester.pump();
    await tester.ensureVisible(find.text('Enter Cart ID instead'));
    await tester.tap(find.text('Enter Cart ID instead')); await tester.pump();
    await tester.enterText(find.byType(TextFormField), 'CART-001');
    await tester.ensureVisible(find.text('Connect Cart'));
    await tester.tap(find.text('Connect Cart')); await tester.pump();
    pairing.pending.completeError(const PairingFailure('cart_already_assigned'));
    await tester.pumpAndSettle();
    expect(pairing.requests, ['CART-001']);
    expect(find.textContaining('another customer'), findsOneWidget);
    expect(find.text('Enter Cart ID'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    auth.dispose(); await carts.dispose();
  });

  testWidgets('old scanner callback is ignored after customer screen disposal', (tester) async {
    late ValueChanged<String?> detect;
    final received = <String>[];
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: CartQrScanner(
      onCart: received.add, onManual: () {},
      cameraBuilder: (callback) { detect = callback; return const SizedBox(); },
    ))));
    await tester.pumpWidget(const SizedBox());
    detect('CART-001');
    expect(received, isEmpty);
  });

  for (final status in [500, 502, 503]) {
    test('server $status is distinguished from network and hides raw response', () async {
      final tokens = TestTokens();
      final service = HttpCartPairingService(tokens: tokens,
        configuration: const PairingConfiguration(baseUrl: 'https://backend.example.test'),
        clientFactory: () => MockClient((request) async {
          expect(request.headers['Authorization'], 'Bearer token-uid-a');
          expect(request.body, '{"cart_id":"CART-001"}');
          return http.Response('<html>private internal error</html>', status);
        }),
      );
      await expectLater(service.pair('CART-001'), throwsA(pairingFailure('server_unavailable')));
      expect(tokens.calls, 1);
      expect(const PairingFailure('server_unavailable').message, isNot(contains('private')));
    });
  }
}
