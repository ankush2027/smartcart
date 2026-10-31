/// Authoritative checkout and payment models.
///
/// All amounts are preserved as integer paise (backend representation).
/// Flutter MUST NOT recalculate authoritative totals.
library;

import '../../../core/data/session_mapper.dart' show rupeesToPaise;

/// Backend states for a checkout row.
///
/// FROZEN  — the checkout is active and awaiting payment.
/// CANCELLED — the checkout was cancelled; session returns to shopping.
/// COMPLETED — payment was verified and accepted by backend.
enum CheckoutState { frozen, cancelled, completed }

/// Backend states for the overall session from read_checkout_state.
enum SessionCheckoutState {
  /// Customer is shopping; no active (FROZEN) checkout.
  shopping,

  /// A checkout is currently FROZEN.
  checkoutFrozen,

  /// Session is fully completed (payment done).
  completed,
}

/// Parsed from `POST /sessions/<sid>/checkout` or `GET /sessions/<sid>/checkout`.
class CheckoutResult {
  const CheckoutResult({
    required this.sessionId,
    required this.cartId,
    required this.checkoutId,
    required this.state,
    required this.finalAmountPaise,
    required this.checkoutStartedAt,
    this.cancelledAt,
    this.runtimeRestartRequired = false,
    this.currency = 'INR',
  });

  final int sessionId;
  final String cartId;
  final int checkoutId;
  final CheckoutState state;

  /// Authoritative final amount in paise. Never recalculated by Flutter.
  final int finalAmountPaise;
  final String checkoutStartedAt;
  final String? cancelledAt;
  final bool runtimeRestartRequired;
  final String currency;

  factory CheckoutResult.fromJson(Map<String, dynamic> json) {
    final rawAmount = json['final_amount'];
    // Backend sends final_amount as a float (INR). Convert to paise.
    final amountPaise = rupeesToPaise(rawAmount);
    final rawState = json['state'] as String? ?? '';
    final state = switch (rawState) {
      'FROZEN' => CheckoutState.frozen,
      'CANCELLED' => CheckoutState.cancelled,
      'COMPLETED' => CheckoutState.completed,
      _ => throw const FormatException('Invalid checkout state'),
    };
    return CheckoutResult(
      sessionId: (json['session_id'] as num).toInt(),
      cartId: json['cart_id'] as String? ?? '',
      checkoutId: (json['checkout_id'] as num).toInt(),
      state: state,
      finalAmountPaise: amountPaise,
      checkoutStartedAt: json['checkout_started_at'] as String? ?? '',
      cancelledAt: json['cancelled_at'] as String?,
      runtimeRestartRequired:
          json['runtime_restart_required'] as bool? ?? false,
      currency: json['currency'] as String? ?? 'INR',
    );
  }
}

/// Payment states exposed by the backend's public_payment allowlist.
enum PaymentState {
  creating,
  orderReady,
  verified,
  completed,
  creationFailed,
  unknown,
}

/// Parsed from `POST/GET /sessions/<sid>/checkouts/<cid>/payment`
/// and from the demo simulate-payment-success response.
class PaymentAttempt {
  const PaymentAttempt({
    required this.paymentAttemptId,
    required this.checkoutId,
    required this.amountPaise,
    required this.currency,
    required this.state,
    this.providerOrderId,
    this.createdAt,
    this.verifiedAt,
    this.completedAt,
    this.errorCode,
  });

  final String paymentAttemptId;
  final int checkoutId;

  /// Authoritative amount in paise from the backend. Never recalculated.
  final int amountPaise;
  final String currency;
  final PaymentState state;
  final String? providerOrderId;
  final String? createdAt;
  final String? verifiedAt;
  final String? completedAt;
  final String? errorCode;

  bool get isCompleted => state == PaymentState.completed;

  factory PaymentAttempt.fromJson(Map<String, dynamic> json) {
    final amount = json['amount_paise'];
    if (amount is! int || amount < 0) {
      throw const FormatException('Invalid payment amount');
    }
    final rawState = json['state'] as String? ?? '';
    final state = switch (rawState) {
      'CREATING' => PaymentState.creating,
      'ORDER_READY' => PaymentState.orderReady,
      'VERIFIED' => PaymentState.verified,
      'COMPLETED' => PaymentState.completed,
      'FAILED' => PaymentState.creationFailed,
      _ => PaymentState.unknown,
    };
    return PaymentAttempt(
      paymentAttemptId: json['payment_attempt_id'] as String,
      checkoutId: (json['checkout_id'] as num).toInt(),
      amountPaise: amount,
      currency: json['currency'] as String? ?? 'INR',
      state: state,
      providerOrderId: json['provider_order_id'] as String?,
      createdAt: json['created_at'] as String?,
      verifiedAt: json['verified_at'] as String?,
      completedAt: json['completed_at'] as String?,
      errorCode: json['error_code'] as String?,
    );
  }
}
