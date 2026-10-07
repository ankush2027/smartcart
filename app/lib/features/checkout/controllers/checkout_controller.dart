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

  // Generation counter guards stale async responses.
  int _generation = 0;

  CheckoutControllerPhase get phase => _phase;
  CheckoutResult? get checkoutResult => _checkoutResult;
  PaymentAttempt? get paymentAttempt => _paymentAttempt;
  String? get error => _error;

  bool get isIdle => !_isBusy;
  bool get _isBusy =>
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
    if (_phase == CheckoutControllerPhase.frozen) return;

    final generation = ++_generation;
    _checkoutResult = null;
    _paymentAttempt = null;
    _setPhase(CheckoutControllerPhase.checkoutInFlight);

    try {
      final result = await _repository.beginCheckout(
        sessionId: sessionId,
        idempotencyKey: idempotencyKey,
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
      _setPhase(CheckoutControllerPhase.shopping, error: f.displayMessage);
    } catch (_) {
      if (_disposed || generation != _generation) return;
      _setPhase(
        CheckoutControllerPhase.shopping,
        error: 'Unable to begin checkout. Please try again.',
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
      _setPhase(CheckoutControllerPhase.frozen, error: f.displayMessage);
    } catch (_) {
      if (_disposed || generation != _generation) return;
      _setPhase(
        CheckoutControllerPhase.frozen,
        error: 'Unable to cancel checkout. Please try again.',
      );
    }
  }

  void _resetToShopping() {
    Future.microtask(() {
      if (!_disposed && _phase == CheckoutControllerPhase.cancelled) {
        _checkoutResult = null;
        _paymentAttempt = null;
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
        idempotencyKey: idempotencyKey,
      );
      if (_disposed || generation != _generation) return;

      _paymentAttempt = attempt;
      if (attempt.state == PaymentState.completed) {
        // Already completed from a previous idempotent call.
        _setPhase(CheckoutControllerPhase.completed);
      } else if (attempt.state == PaymentState.creationFailed) {
        _setPhase(
          CheckoutControllerPhase.frozen,
          error: 'Payment order could not be created. Please try again.',
        );
      } else {
        _setPhase(CheckoutControllerPhase.paymentReady);
      }
    } on CheckoutFailure catch (f) {
      if (_disposed || generation != _generation) return;
      _setPhase(CheckoutControllerPhase.frozen, error: f.displayMessage);
    } catch (_) {
      if (_disposed || generation != _generation) return;
      _setPhase(
        CheckoutControllerPhase.frozen,
        error: 'Unable to start payment. Please try again.',
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
      _setPhase(CheckoutControllerPhase.paymentReady, error: f.displayMessage);
    } catch (_) {
      if (_disposed || generation != _generation) return;
      _setPhase(
        CheckoutControllerPhase.paymentReady,
        error: 'Demo payment failed. Please try again.',
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
