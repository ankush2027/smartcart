/// CheckoutController — server-authoritative checkout/payment state machine.
///
/// Lifecycle:
///   LIVE CART → CHECKOUT REQUEST → FROZEN → PAYMENT → COMPLETED
///   FROZEN → CANCEL → CANCELLED → LIVE CART
///
/// All state transitions are driven by backend responses.
/// Flutter MUST NOT approve payment locally.
library;

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../data/checkout_repository.dart';
import '../models/checkout_models.dart';

enum CheckoutControllerPhase {
  /// Customer is actively shopping; no checkout in progress.
  shopping,
  recovering,
  recoveryRequired,

  /// Checkout freeze request in flight.
  checkoutInFlight,

  /// Backend has confirmed FROZEN checkout; waiting for payment.
  frozen,

  /// Cancel request in flight.
  cancelInFlight,

  /// Checkout was cancelled; back to shopping.
  cancelled,

  /// Start payment request in flight.
  paymentInFlight,

  /// Payment order created; demo payment ready to be triggered.
  paymentReady,

  /// Demo simulate-payment request in flight.
  demoPaymentInFlight,

  /// Backend confirmed COMPLETED payment. Terminal state — authoritative.
  completed,
}

class CheckoutController extends ChangeNotifier {
  CheckoutController(this._repository);

  final CheckoutRepository _repository;

  CheckoutControllerPhase _phase = CheckoutControllerPhase.shopping;
  CheckoutResult? _checkoutResult;
  PaymentAttempt? _paymentAttempt;
  String? _error;
  bool _disposed = false;
  String? _checkoutKey;
  String? _paymentKey;
  List<Map<String, dynamic>> items = const [];

  Future<void> recover(int sessionId) async {
    if (_disposed || _isBusy) return;
    final generation = ++_generation;
    _setPhase(CheckoutControllerPhase.recovering);
    try {
      await _readState(sessionId, generation);
    } catch (_) {
      if (!_disposed && generation == _generation) {
        _setPhase(
          CheckoutControllerPhase.recoveryRequired,
          error: 'Unable to confirm checkout state. Check your connection and retry.',
        );
      }
    }
  }

  Future<void> _readState(int sessionId, int generation) async {
    final data = await _repository.readCheckoutState(sessionId: sessionId);
    if (_disposed || generation != _generation) return;
    final state = data['state'];
    if (state == 'SHOPPING') {
      _checkoutResult = null;
      _paymentAttempt = null;
      items = const [];
      _setPhase(CheckoutControllerPhase.shopping);
      return;
    }
    if (state != 'CHECKOUT_FROZEN' && state != 'COMPLETED') {
      throw const FormatException('Invalid checkout state');
    }
    final result = CheckoutResult.fromJson(
      Map<String, dynamic>.from(data['latest_checkout'] as Map),
    );
    if (result.sessionId != sessionId) {
      throw const FormatException('Session mismatch');
    }
    _checkoutResult = result;
    items = (data['items'] as List? ?? const [])
        .map((item) => Map<String, dynamic>.from(item as Map))
        .toList();
    PaymentAttempt? attempt;
    try {
      attempt = await _repository.paymentStatus(
        sessionId: sessionId,
        checkoutId: result.checkoutId,
      );
    } on CheckoutFailure catch (failure) {
      if (failure.code != 'payment_not_found' || state == 'COMPLETED') rethrow;
    }
    if (_disposed || generation != _generation) return;
    _paymentAttempt = attempt;
    if (attempt == null) {
      _setPhase(CheckoutControllerPhase.frozen);
    } else {
      _acceptPayment(attempt);
    }
  }

  void _acceptPayment(PaymentAttempt attempt) {
    if (attempt.checkoutId != _checkoutResult?.checkoutId) {
      throw const FormatException('Checkout mismatch');
    }
    _paymentAttempt = attempt;
    if (attempt.isCompleted) {
      _setPhase(CheckoutControllerPhase.completed);
    } else if (attempt.state == PaymentState.orderReady ||
        attempt.state == PaymentState.creating ||
        attempt.state == PaymentState.verified) {
      _setPhase(CheckoutControllerPhase.paymentReady);
    } else {
      _setPhase(
        CheckoutControllerPhase.recoveryRequired,
        error: attempt.state == PaymentState.creationFailed
            ? 'Payment was rejected. Contact the cart operator; this reservation cannot be cancelled.'
            : 'Payment is still being confirmed. Refresh its status before continuing.',
      );
    }
  }

  Future<void> _reconcileFailure(
    int sessionId,
    int generation,
    String message,
  ) async {
    try {
      await _readState(sessionId, generation);
      if (!_disposed &&
          generation == _generation &&
          _phase != CheckoutControllerPhase.completed) {
        _error = message;
        _notify();
      }
    } catch (_) {
      if (!_disposed && generation == _generation) {
        _setPhase(CheckoutControllerPhase.recoveryRequired, error: message);
      }
    }
  }

  // Generation counter guards stale async responses.
  int _generation = 0;

  CheckoutControllerPhase get phase => _phase;
  CheckoutResult? get checkoutResult => _checkoutResult;
  PaymentAttempt? get paymentAttempt => _paymentAttempt;
  String? get error => _error;

  bool get isIdle => !_isBusy;
  bool get _isBusy =>
      _phase == CheckoutControllerPhase.recovering ||
      _phase == CheckoutControllerPhase.checkoutInFlight ||
      _phase == CheckoutControllerPhase.cancelInFlight ||
      _phase == CheckoutControllerPhase.paymentInFlight ||
      _phase == CheckoutControllerPhase.demoPaymentInFlight;

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  void _setPhase(CheckoutControllerPhase phase, {String? error}) {
    _phase = phase;
    _error = error;
    _notify();
  }

  // ---------------------------------------------------------------------------
  // Checkout freeze
  // ---------------------------------------------------------------------------

  /// Begin checkout freeze. [sessionId] must come from the active cart session.
  /// [idempotencyKey] should be a UUID generated at call site, not reused.
  ///
  /// Duplicate-tap protected: returns immediately if a request is already in
  /// flight or the checkout is already frozen.
  Future<void> beginCheckout({
    required int sessionId,
    required String idempotencyKey,
  }) async {
    if (_disposed) return;
    if (_isBusy) return; // Duplicate-tap protection.
    if (_phase != CheckoutControllerPhase.shopping) return;

    final generation = ++_generation;
    _checkoutResult = null;
    _paymentAttempt = null;
    _setPhase(CheckoutControllerPhase.checkoutInFlight);

    try {
      final result = await _repository.beginCheckout(
        sessionId: sessionId,
        idempotencyKey: _checkoutKey ??= idempotencyKey,
      );
      if (_disposed || generation != _generation) return;

      _checkoutResult = result;
      if (result.state == CheckoutState.frozen) {
        _setPhase(CheckoutControllerPhase.frozen);
      } else {
        // Unexpected state — surface as error.
        _setPhase(
          CheckoutControllerPhase.shopping,
          error: 'Unexpected checkout state: ${result.state}',
        );
      }
    } on CheckoutFailure catch (f) {
      if (_disposed || generation != _generation) return;
      await _reconcileFailure(sessionId, generation, f.displayMessage);
    } catch (_) {
      if (_disposed || generation != _generation) return;
      await _reconcileFailure(
        sessionId,
        generation,
        'Unable to confirm checkout. Retry status.',
      );
    }
  }

  // ---------------------------------------------------------------------------
  // Cancellation
  // ---------------------------------------------------------------------------

  /// Cancel the current frozen checkout. Backend is authoritative.
  ///
  /// Duplicate-tap protected; will not run if a request is in flight.
  /// Does not locally unlock before backend success.
  Future<void> cancelCheckout({
    required int sessionId,
    required int checkoutId,
  }) async {
    if (_disposed) return;
    if (_isBusy) return; // Duplicate-tap protection.
    if (_phase != CheckoutControllerPhase.frozen &&
        _phase != CheckoutControllerPhase.paymentReady) {
      return;
    }

    final generation = ++_generation;
    _setPhase(CheckoutControllerPhase.cancelInFlight);

    try {
      final result = await _repository.cancelCheckout(
        sessionId: sessionId,
        checkoutId: checkoutId,
      );
      if (_disposed || generation != _generation) return;

      if (result.state == CheckoutState.cancelled) {
        _checkoutResult = result;
        _paymentAttempt = null;
        _setPhase(CheckoutControllerPhase.cancelled);
        // Auto-return to shopping so the customer can proceed.
        _resetToShopping();
      } else {
        // Backend rejected cancellation (e.g., already completed).
        _setPhase(
          CheckoutControllerPhase.frozen,
          error:
              'Cancellation rejected by server. Checkout may have progressed.',
        );
      }
    } on CheckoutFailure catch (f) {
      if (_disposed || generation != _generation) return;
      // Restore to frozen so user can retry or see current state.
      await _reconcileFailure(sessionId, generation, f.displayMessage);
    } catch (_) {
      if (_disposed || generation != _generation) return;
      await _reconcileFailure(
        sessionId,
        generation,
        'Unable to confirm cancellation. Retry status.',
      );
    }
  }

  void _resetToShopping() {
    Future.microtask(() {
      if (!_disposed && _phase == CheckoutControllerPhase.cancelled) {
        _checkoutResult = null;
        _paymentAttempt = null;
        _checkoutKey = null;
        _paymentKey = null;
        items = const [];
        _setPhase(CheckoutControllerPhase.shopping);
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Payment start
  // ---------------------------------------------------------------------------

  /// Start a payment attempt. Duplicate-tap protected.
  Future<void> startPayment({
    required int sessionId,
    required int checkoutId,
    required String idempotencyKey,
  }) async {
    if (_disposed) return;
    if (_isBusy) return; // Duplicate-tap protection.
    if (_phase != CheckoutControllerPhase.frozen) return;

    final generation = ++_generation;
    _setPhase(CheckoutControllerPhase.paymentInFlight);

    try {
      final attempt = await _repository.startPayment(
        sessionId: sessionId,
        checkoutId: checkoutId,
        idempotencyKey: _paymentKey ??= idempotencyKey,
      );
      if (_disposed || generation != _generation) return;

      _acceptPayment(attempt);
    } on CheckoutFailure catch (f) {
      if (_disposed || generation != _generation) return;
      await _reconcileFailure(sessionId, generation, f.displayMessage);
    } catch (_) {
      if (_disposed || generation != _generation) return;
      await _reconcileFailure(
        sessionId,
        generation,
        'Unable to confirm payment. Retry status.',
      );
    }
  }

  // ---------------------------------------------------------------------------
  // Demo payment completion — DEMO ONLY
  // ---------------------------------------------------------------------------

  /// Triggers the backend FakeProvider pipeline.
  ///
  /// Payment success is confirmed ONLY by the backend COMPLETED response.
  /// Flutter does NOT set payment success locally. Duplicate-tap protected.
  Future<void> simulateDemoPayment({
    required int sessionId,
    required int checkoutId,
  }) async {
    if (_disposed) return;
    if (_isBusy) return; // Duplicate-tap protection.
    if (_phase != CheckoutControllerPhase.paymentReady) return;

    final generation = ++_generation;
    _setPhase(CheckoutControllerPhase.demoPaymentInFlight);

    try {
      final attempt = await _repository.simulateDemoPaymentSuccess(
        sessionId: sessionId,
        checkoutId: checkoutId,
      );
      if (_disposed || generation != _generation) return;

      _paymentAttempt = attempt;

      // Only backend-COMPLETED is accepted as payment success.
      if (attempt.isCompleted) {
        _setPhase(CheckoutControllerPhase.completed);
      } else {
        _setPhase(
          CheckoutControllerPhase.paymentReady,
          error: 'Demo payment did not reach COMPLETED state. Please retry.',
        );
      }
    } on CheckoutFailure catch (f) {
      if (_disposed || generation != _generation) return;
      await _reconcileFailure(sessionId, generation, f.displayMessage);
    } catch (_) {
      if (_disposed || generation != _generation) return;
      await _reconcileFailure(
        sessionId,
        generation,
        'Unable to confirm payment. Retry status.',
      );
    }
  }

  // ---------------------------------------------------------------------------
  // Reset (for stale-state recovery or new session)
  // ---------------------------------------------------------------------------

  /// Reset the controller to shopping phase. Cancels any in-flight generation.
  void reset() {
    if (_disposed) return;
    _generation++; // Invalidate pending async operations.
    _checkoutKey = null;
    _paymentKey = null;
    items = const [];
    _checkoutResult = null;
    _paymentAttempt = null;
    _setPhase(CheckoutControllerPhase.shopping);
  }

  @override
  void dispose() {
    _disposed = true;
    _generation++;
    super.dispose();
  }
}
