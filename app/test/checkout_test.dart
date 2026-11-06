/// Checkout and payment integration tests.
///
/// Tests the full checkout/payment controller lifecycle.
/// No real Firebase, real backend, or real Razorpay required.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:smartcart/features/checkout/controllers/checkout_controller.dart';
import 'package:smartcart/features/checkout/data/checkout_repository.dart';
import 'package:smartcart/features/checkout/data/http_checkout_repository.dart';
import 'package:smartcart/features/checkout/models/checkout_models.dart';

// ---------------------------------------------------------------------------
// Fake repository for controller tests
// ---------------------------------------------------------------------------

class FakeCheckoutRepository implements CheckoutRepository {
  // Configurable responses
  Future<CheckoutResult> Function()? beginCheckoutResponse;
  Future<CheckoutResult> Function()? cancelCheckoutResponse;
  Future<Map<String, dynamic>> Function()? readCheckoutStateResponse;
  Future<PaymentAttempt> Function()? startPaymentResponse;
  Future<PaymentAttempt> Function()? paymentStatusResponse;
  Future<PaymentAttempt> Function()? simulateDemoPaymentResponse;

  final checkoutKeys = <String>[];
  int beginCheckoutCalls = 0;
  int cancelCheckoutCalls = 0;
  int startPaymentCalls = 0;
  int simulateDemoPaymentCalls = 0;

  @override
  Future<CheckoutResult> beginCheckout({
    required int sessionId,
    required String idempotencyKey,
  }) {
    beginCheckoutCalls++;
    checkoutKeys.add(idempotencyKey);
    return beginCheckoutResponse?.call() ??
        Future.error(const CheckoutFailure('network'));
  }

  @override
  Future<CheckoutResult> cancelCheckout({
    required int sessionId,
    required int checkoutId,
  }) {
    cancelCheckoutCalls++;
    return cancelCheckoutResponse?.call() ??
        Future.error(const CheckoutFailure('network'));
  }

  @override
  Future<Map<String, dynamic>> readCheckoutState({required int sessionId}) {
    return readCheckoutStateResponse?.call() ??
        Future.value({'state': 'SHOPPING'});
  }

  @override
  Future<PaymentAttempt> startPayment({
    required int sessionId,
    required int checkoutId,
    required String idempotencyKey,
  }) {
    startPaymentCalls++;
    return startPaymentResponse?.call() ??
        Future.error(const CheckoutFailure('network'));
  }

  @override
  Future<PaymentAttempt> paymentStatus({
    required int sessionId,
    required int checkoutId,
  }) {
    return paymentStatusResponse?.call() ??
        Future.error(const CheckoutFailure('network'));
  }

  @override
  Future<PaymentAttempt> simulateDemoPaymentSuccess({
    required int sessionId,
    required int checkoutId,
  }) {
    simulateDemoPaymentCalls++;
    return simulateDemoPaymentResponse?.call() ??
        Future.error(const CheckoutFailure('network'));
  }
}

// ---------------------------------------------------------------------------
// Fake token provider for HTTP tests
// ---------------------------------------------------------------------------

class FakeTokenProvider implements CheckoutTokenProvider {
  @override
  String? currentUid = 'uid-test';
  Object? error;

  @override
  Future<String> freshIdToken() async {
    if (error != null) throw error!;
    return 'fake-token';
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

CheckoutResult frozenCheckout({int checkoutId = 42, double amount = 250.0}) =>
    CheckoutResult(
      sessionId: 1,
      cartId: 'CART-001',
      checkoutId: checkoutId,
      state: CheckoutState.frozen,
      finalAmountPaise: (amount * 100).round(),
      checkoutStartedAt: '2024-01-01 10:00:00',
    );

CheckoutResult cancelledCheckout({int checkoutId = 42}) => CheckoutResult(
  sessionId: 1,
  cartId: 'CART-001',
  checkoutId: checkoutId,
  state: CheckoutState.cancelled,
  finalAmountPaise: 25000,
  checkoutStartedAt: '2024-01-01 10:00:00',
  cancelledAt: '2024-01-01 10:05:00',
);

PaymentAttempt orderReadyAttempt({int checkoutId = 42, int paise = 25000}) =>
    PaymentAttempt(
      paymentAttemptId: '99ab0123456789abcdef0123456789abcd',
      checkoutId: checkoutId,
      amountPaise: paise,
      currency: 'INR',
      state: PaymentState.orderReady,
      providerOrderId: 'order_fake123',
      createdAt: '2024-01-01 10:01:00',
    );

PaymentAttempt completedAttempt({int checkoutId = 42, int paise = 25000}) =>
    PaymentAttempt(
      paymentAttemptId: '99ab0123456789abcdef0123456789abcd',
      checkoutId: checkoutId,
      amountPaise: paise,
      currency: 'INR',
      state: PaymentState.completed,
      providerOrderId: 'order_fake123',
      createdAt: '2024-01-01 10:01:00',
      completedAt: '2024-01-01 10:02:00',
    );

// ---------------------------------------------------------------------------
// Test 1: Checkout success
// ---------------------------------------------------------------------------

void main() {
  group('CheckoutController', () {
    late FakeCheckoutRepository repo;
    late CheckoutController controller;

    setUp(() {
      repo = FakeCheckoutRepository();
      controller = CheckoutController(repo);
    });

    tearDown(() => controller.dispose());

    // --- Test 1: checkout success ---
    test('beginCheckout transitions to frozen on success', () async {
      repo.beginCheckoutResponse = () async => frozenCheckout();

      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'key-1');

      expect(controller.phase, CheckoutControllerPhase.frozen);
      expect(controller.checkoutResult?.checkoutId, 42);
      expect(controller.error, isNull);
    });

    // --- Test 2: checkout rejection ---
    test('beginCheckout shows error on checkout_already_frozen', () async {
      repo.beginCheckoutResponse = () async =>
          throw const CheckoutFailure('checkout_already_frozen');
      repo.readCheckoutStateResponse = () async =>
          throw const CheckoutFailure('network');

      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'key-2');

      expect(controller.phase, CheckoutControllerPhase.recoveryRequired);
      expect(controller.error, contains('already'));
    });

    // --- Test 3: duplicate Checkout protection ---
    test('duplicate beginCheckout calls are ignored while in-flight', () async {
      final completer = Completer<CheckoutResult>();
      repo.beginCheckoutResponse = () => completer.future;

      // Start first call (in-flight)
      final first = controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');
      expect(controller.phase, CheckoutControllerPhase.checkoutInFlight);

      // Second call should be silently dropped (no new in-flight counter)
      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k2');
      expect(repo.beginCheckoutCalls, 1);

      completer.complete(frozenCheckout());
      await first;
      expect(controller.phase, CheckoutControllerPhase.frozen);
    });

    // --- Test 4: cancellation success ---
    test('cancelCheckout transitions back to shopping on success', () async {
      repo.beginCheckoutResponse = () async => frozenCheckout();
      repo.cancelCheckoutResponse = () async => cancelledCheckout();

      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');
      expect(controller.phase, CheckoutControllerPhase.frozen);

      await controller.cancelCheckout(sessionId: 1, checkoutId: 42);

      // After cancelled, auto-resets to shopping via microtask.
      await Future.microtask(() {});
      expect(controller.phase, CheckoutControllerPhase.shopping);
      expect(controller.error, isNull);
    });

    // --- Test 5: cancellation failure ---
    test('cancelCheckout shows error and stays frozen on failure', () async {
      repo.beginCheckoutResponse = () async => frozenCheckout();
      repo.cancelCheckoutResponse = () async =>
          throw const CheckoutFailure('payment_already_started');
      repo.readCheckoutStateResponse = () async =>
          throw const CheckoutFailure('network');

      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');
      await controller.cancelCheckout(sessionId: 1, checkoutId: 42);

      expect(controller.phase, CheckoutControllerPhase.recoveryRequired);
      expect(controller.error, isNotNull);
    });

    // --- Test 6: payment start success ---
    test('startPayment transitions to paymentReady', () async {
      repo.beginCheckoutResponse = () async => frozenCheckout();
      repo.startPaymentResponse = () async => orderReadyAttempt();

      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');
      await controller.startPayment(
        sessionId: 1,
        checkoutId: 42,
        idempotencyKey: 'pay-key',
      );

      expect(controller.phase, CheckoutControllerPhase.paymentReady);
      expect(controller.paymentAttempt?.providerOrderId, 'order_fake123');
    });

    // --- Test 7: backend-authoritative demo payment completion ---
    test('simulateDemoPayment completes only on backend COMPLETED', () async {
      repo.beginCheckoutResponse = () async => frozenCheckout();
      repo.startPaymentResponse = () async => orderReadyAttempt();
      repo.simulateDemoPaymentResponse = () async => completedAttempt();

      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');
      await controller.startPayment(
        sessionId: 1,
        checkoutId: 42,
        idempotencyKey: 'pk',
      );
      await controller.simulateDemoPayment(sessionId: 1, checkoutId: 42);

      // CRITICAL: Flutter only accepts COMPLETED from backend.
      expect(controller.phase, CheckoutControllerPhase.completed);
      expect(controller.paymentAttempt?.isCompleted, isTrue);
      expect(controller.paymentAttempt?.amountPaise, 25000);
    });

    // --- Test 8: payment rejection ---
    test('startPayment shows error on payment_unavailable', () async {
      repo.beginCheckoutResponse = () async => frozenCheckout();
      repo.startPaymentResponse = () async =>
          throw const CheckoutFailure('payment_unavailable');
      repo.readCheckoutStateResponse = () async =>
          throw const CheckoutFailure('network');

      await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');
      await controller.startPayment(
        sessionId: 1,
        checkoutId: 42,
        idempotencyKey: 'pk',
      );

      expect(controller.phase, CheckoutControllerPhase.recoveryRequired);
      expect(controller.error, contains('unavailable'));
    });

    // --- Test 9: duplicate Pay protection ---
    test(
      'duplicate simulateDemoPayment calls are ignored while in-flight',
      () async {
        final completer = Completer<PaymentAttempt>();
        repo.beginCheckoutResponse = () async => frozenCheckout();
        repo.startPaymentResponse = () async => orderReadyAttempt();
        repo.simulateDemoPaymentResponse = () => completer.future;

        await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');
        await controller.startPayment(
          sessionId: 1,
          checkoutId: 42,
          idempotencyKey: 'pk',
        );

        final first = controller.simulateDemoPayment(
          sessionId: 1,
          checkoutId: 42,
        );
        expect(controller.phase, CheckoutControllerPhase.demoPaymentInFlight);

        // Duplicate tap — should be dropped.
        await controller.simulateDemoPayment(sessionId: 1, checkoutId: 42);
        expect(repo.simulateDemoPaymentCalls, 1);

        completer.complete(completedAttempt());
        await first;
        expect(controller.phase, CheckoutControllerPhase.completed);
      },
    );

    // --- Test 10: network failure ---
    test(
      'network failure on beginCheckout shows user-friendly error',
      () async {
        repo.beginCheckoutResponse = () async =>
            throw const CheckoutFailure('network');

        await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');

        expect(controller.phase, CheckoutControllerPhase.shopping);
        expect(controller.error, contains('connection'));
      },
    );

    // --- Test 11: authoritative amount handling (paise) ---
    test(
      'checkout preserves server amount in paise without recalculation',
      () async {
        // Server returns 123.45 INR = 12345 paise.
        repo.beginCheckoutResponse = () async => frozenCheckout(amount: 123.45);

        await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');

        // Flutter stores the amount as returned by backend.
        expect(controller.checkoutResult?.finalAmountPaise, 12345);
      },
    );

    // --- Test 12: completed payment state ---
    test(
      'already-completed payment from startPayment transitions to completed',
      () async {
        repo.beginCheckoutResponse = () async => frozenCheckout();
        // Backend returns COMPLETED on payment/start (idempotent replay).
        repo.startPaymentResponse = () async => completedAttempt();

        await controller.beginCheckout(sessionId: 1, idempotencyKey: 'k');
        await controller.startPayment(
          sessionId: 1,
          checkoutId: 42,
          idempotencyKey: 'pk',
        );

        expect(controller.phase, CheckoutControllerPhase.completed);
      },
    );

    // --- Test 13: stale response protection ---
    test('stale response after reset is ignored', () async {
      final completer = Completer<CheckoutResult>();
      repo.beginCheckoutResponse = () => completer.future;

      final inFlight = controller.beginCheckout(
        sessionId: 1,
        idempotencyKey: 'k',
      );

      // Reset invalidates current generation before response arrives.
      controller.reset();
      expect(controller.phase, CheckoutControllerPhase.shopping);

      completer.complete(frozenCheckout());
      await inFlight;

      // Stale response must not transition to frozen.
      expect(controller.phase, CheckoutControllerPhase.shopping);
    });
  });

  // ---------------------------------------------------------------------------
  // HTTP repository tests (mock HTTP client)
  // ---------------------------------------------------------------------------

  group('HttpCheckoutRepository', () {
    late FakeTokenProvider tokens;

    HttpCheckoutRepository makeRepo(http.Client client) =>
        HttpCheckoutRepository(
          tokens: tokens,
          configuration: const BackendConfiguration(
            baseUrl: 'http://localhost:8000',
          ),
          clientFactory: () => client,
          timeout: const Duration(seconds: 5),
        );

    setUp(() {
      tokens = FakeTokenProvider();
    });

    Map<String, String> jsonHeader() => {'content-type': 'application/json'};

    Map<String, dynamic> checkoutJson({
      String state = 'FROZEN',
      int checkoutId = 10,
    }) => {
      'session_id': 1,
      'cart_id': 'CART-001',
      'checkout_id': checkoutId,
      'state': state,
      'final_amount': 250.0,
      'checkout_started_at': '2024-01-01 10:00:00',
      'cancelled_at': null,
      'runtime_restart_required': false,
      'currency': 'INR',
    };

    Map<String, dynamic> paymentJson({
      String state = 'ORDER_READY',
      int amountPaise = 25000,
    }) => {
      'payment_attempt_id': '99ab0123456789abcdef0123456789abcd',
      'checkout_id': 10,
      'amount_paise': amountPaise,
      'currency': 'INR',
      'state': state,
      'provider_order_id': 'order_abc',
      'created_at': '2024-01-01 10:01:00',
      'verified_at': null,
      'completed_at': null,
      'error_code': null,
    };

    test('beginCheckout posts to correct URL with auth header', () async {
      Uri? captured;
      String? capturedAuth;
      final client = MockClient((req) async {
        captured = req.url;
        capturedAuth = req.headers['Authorization'];
        return http.Response(
          jsonEncode(checkoutJson()),
          200,
          headers: jsonHeader(),
        );
      });

      final repo = makeRepo(client);
      final result = await repo.beginCheckout(
        sessionId: 1,
        idempotencyKey: 'key-http-1',
      );

      expect(captured?.path, '/sessions/1/checkout');
      expect(capturedAuth, 'Bearer fake-token');
      expect(result.checkoutId, 10);
      expect(result.state, CheckoutState.frozen);
    });

    test('cancelCheckout posts to correct URL', () async {
      Uri? captured;
      final client = MockClient((req) async {
        captured = req.url;
        return http.Response(
          jsonEncode(checkoutJson(state: 'CANCELLED')),
          200,
          headers: jsonHeader(),
        );
      });

      await makeRepo(client).cancelCheckout(sessionId: 1, checkoutId: 10);
      expect(captured?.path, '/sessions/1/checkouts/10/cancel');
    });

    test('startPayment posts to correct URL', () async {
      Uri? captured;
      final client = MockClient((req) async {
        captured = req.url;
        return http.Response(
          jsonEncode(paymentJson()),
          200,
          headers: jsonHeader(),
        );
      });

      final attempt = await makeRepo(client).startPayment(
        sessionId: 1,
        checkoutId: 10,
        idempotencyKey: 'pay-key-http',
      );

      expect(captured?.path, '/sessions/1/checkouts/10/payment');
      expect(attempt.amountPaise, 25000);
      expect(attempt.state, PaymentState.orderReady);
    });

    test('simulateDemoPaymentSuccess posts to demo endpoint', () async {
      Uri? captured;
      final client = MockClient((req) async {
        captured = req.url;
        return http.Response(
          jsonEncode(paymentJson(state: 'COMPLETED')),
          200,
          headers: jsonHeader(),
        );
      });

      final attempt = await makeRepo(client)
          .simulateDemoPaymentSuccess(sessionId: 1, checkoutId: 10);

      expect(
        captured?.path,
        '/demo/sessions/1/checkouts/10/simulate-payment-success',
      );
      expect(attempt.isCompleted, isTrue);
    });

    test('HTTP 401 throws authentication_required', () async {
      final client = MockClient(
        (_) async => http.Response(
          jsonEncode({'error': 'authentication_required', 'message': 'No'}),
          401,
          headers: jsonHeader(),
        ),
      );

      expect(
        () => makeRepo(client).beginCheckout(sessionId: 1, idempotencyKey: 'k'),
        throwsA(
          isA<CheckoutFailure>().having(
            (f) => f.code,
            'code',
            'authentication_required',
          ),
        ),
      );
    });

    test('backend error code is surfaced as CheckoutFailure', () async {
      final client = MockClient(
        (_) async => http.Response(
          jsonEncode({
            'error': 'checkout_already_frozen',
            'message': 'Already frozen.',
          }),
          409,
          headers: jsonHeader(),
        ),
      );

      expect(
        () => makeRepo(client).beginCheckout(sessionId: 1, idempotencyKey: 'k'),
        throwsA(
          isA<CheckoutFailure>().having(
            (f) => f.code,
            'code',
            'checkout_already_frozen',
          ),
        ),
      );
    });

    test('paise amount is preserved exactly from backend', () async {
      // Backend sends amount_paise: 12345 -> Flutter stores 12345.
      final client = MockClient(
        (_) async => http.Response(
          jsonEncode(paymentJson(amountPaise: 12345)),
          200,
          headers: jsonHeader(),
        ),
      );

      final attempt = await makeRepo(client)
          .startPayment(sessionId: 1, checkoutId: 10, idempotencyKey: 'k');

      expect(attempt.amountPaise, 12345);
    });

    test('token fetch failure throws authentication_required', () async {
      tokens.error = const CheckoutFailure('authentication_required');
      final client = MockClient(
        (_) async => http.Response('{}', 200, headers: jsonHeader()),
      );

      expect(
        () => makeRepo(client).beginCheckout(sessionId: 1, idempotencyKey: 'k'),
        throwsA(isA<CheckoutFailure>()),
      );
    });

    test(
      'demo payment not completed state does not count as success',
      () async {
        // Backend returns ORDER_READY instead of COMPLETED.
        final client = MockClient(
          (_) async => http.Response(
            jsonEncode(paymentJson(state: 'ORDER_READY')),
            200,
            headers: jsonHeader(),
          ),
        );

        final attempt = await makeRepo(client)
            .simulateDemoPaymentSuccess(sessionId: 1, checkoutId: 10);

        // Controller would see isCompleted = false and show an error.
        expect(attempt.isCompleted, isFalse);
      },
    );
  });
}
