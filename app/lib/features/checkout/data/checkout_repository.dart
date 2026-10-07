/// Abstract checkout and payment repository.
///
/// The backend is authoritative for all amounts, states, and payment results.
/// Flutter never calculates totals or decides payment success locally.
library;

import '../models/checkout_models.dart';

/// Failure from the checkout/payment HTTP layer.
class CheckoutFailure implements Exception {
  const CheckoutFailure(this.code, [this.message]);
  final String code;
  final String? message;

  String get displayMessage => switch (code) {
    'authentication_required' ||
    'invalid_token' => 'Please sign in again to continue.',
    'checkout_already_frozen' => 'A checkout is already in progress.',
    'empty_cart' => 'Add items to your cart before checking out.',
    'payment_unavailable' => 'Payment service is currently unavailable.',
    'checkout_not_found' => 'Checkout not found. Please start again.',
    'session_completed' => 'This session has already been completed.',
    'payment_already_started' =>
      'Payment has already been started for this checkout.',
    'demo_payment_failed' => 'Demo payment failed. Please try again.',
    'network' => 'Cannot reach the server. Check your connection.',
    'timeout' => 'Request timed out. Please try again.',
    _ => message ?? 'Something went wrong. Please try again.',
  };
}

/// Token provider — callers get a fresh Firebase ID token for every request.
/// Tokens are never stored persistently.
abstract interface class CheckoutTokenProvider {
  String? get currentUid;
  Future<String> freshIdToken();
}

/// Repository contract for checkout and payment operations.
abstract interface class CheckoutRepository {
  /// `POST /sessions/<sid>/checkout`
  /// Returns authoritative [CheckoutResult].
  Future<CheckoutResult> beginCheckout({
    required int sessionId,
    required String idempotencyKey,
  });

  /// `POST /sessions/<sid>/checkouts/<cid>/cancel`
  Future<CheckoutResult> cancelCheckout({
    required int sessionId,
    required int checkoutId,
  });

  /// `GET /sessions/<sid>/checkout`
  Future<Map<String, dynamic>> readCheckoutState({required int sessionId});

  /// `POST /sessions/<sid>/checkouts/<cid>/payment`
  Future<PaymentAttempt> startPayment({
    required int sessionId,
    required int checkoutId,
    required String idempotencyKey,
  });

  /// `GET /sessions/<sid>/checkouts/<cid>/payment`
  Future<PaymentAttempt> paymentStatus({
    required int sessionId,
    required int checkoutId,
  });

  /// `POST /demo/sessions/<sid>/checkouts/<cid>/simulate-payment-success`
  ///
  /// DEMO ONLY: triggers FakeProvider pipeline on the backend.
  /// Payment success is confirmed by the backend response, never by Flutter.
  Future<PaymentAttempt> simulateDemoPaymentSuccess({
    required int sessionId,
    required int checkoutId,
  });
}
