/// CheckoutPanel — functional checkout/payment UI panel.
///
/// Renders the minimum required controls for:
///   Checkout → Cancel → Start Payment → Complete Demo Payment → Success
///
/// This panel is intentionally functional, not visually redesigned.
/// Final UI design is handled separately.
///
/// DEMO PAYMENT is clearly labeled throughout.
/// Payment success requires backend COMPLETED confirmation — never local.
library;

import 'package:flutter/material.dart';

import '../../customer/models/customer_cart.dart';
import '../controllers/checkout_controller.dart';
import '../models/checkout_models.dart';

class CheckoutPanel extends StatelessWidget {
  const CheckoutPanel({
    super.key,
    required this.controller,
    required this.sessionId,
    required this.totalPaise,
    required this.isDemo,
    this.onCheckout,
    this.onRefresh,
    this.onCancel,
    this.onStartPayment,
    this.onDemoPayment,
  });

  final CheckoutController controller;
  final int sessionId;
  final int totalPaise;

  /// Whether we are in demo mode (shows demo payment button).
  final bool isDemo;

  // Optional overrides for idempotency key generation (for testing).
  final Future<void> Function()? onCheckout;
  final Future<void> Function()? onRefresh;
  final Future<void> Function()? onCancel;
  final Future<void> Function()? onStartPayment;
  final Future<void> Function()? onDemoPayment;

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: controller,
      builder: (context, _) => _build(context),
    );
  }

  Widget _build(BuildContext context) {
    final phase = controller.phase;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (controller.error != null) ...[
          Semantics(
            liveRegion: true,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              decoration: BoxDecoration(
                color: Theme.of(context).colorScheme.errorContainer,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Text(
                controller.error!,
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onErrorContainer,
                ),
              ),
            ),
          ),
          const SizedBox(height: 12),
        ],
        ..._phaseContent(context, phase),
      ],
    );
  }

  List<Widget> _phaseContent(
    BuildContext context,
    CheckoutControllerPhase phase,
  ) {
    switch (phase) {
      case CheckoutControllerPhase.recovering:
        return [const _LoadingRow(label: 'Checking checkout status…')];
      case CheckoutControllerPhase.recoveryRequired:
        return [
          FilledButton(onPressed: onRefresh, child: const Text('Retry status')),
        ];
      case CheckoutControllerPhase.shopping:
        return [
          FilledButton.icon(
            key: const Key('checkout_button'),
            onPressed: onCheckout,
            icon: const Icon(Icons.shopping_cart_checkout),
            label: const Text('Checkout'),
          ),
        ];

      case CheckoutControllerPhase.checkoutInFlight:
        return [const _LoadingRow(label: 'Requesting checkout…')];

      case CheckoutControllerPhase.frozen:
      case CheckoutControllerPhase.paymentInFlight:
        final result = controller.checkoutResult;
        return [
          _CheckoutSummaryCard(result: result, totalPaise: totalPaise),
          const SizedBox(height: 12),
          FilledButton.icon(
            key: const Key('start_payment_button'),
            onPressed: phase == CheckoutControllerPhase.frozen
                ? onStartPayment
                : null,
            icon: const Icon(Icons.payment),
            label: phase == CheckoutControllerPhase.paymentInFlight
                ? const _LoadingLabel('Starting payment…')
                : const Text('Pay Now'),
          ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            key: const Key('cancel_checkout_button'),
            onPressed: phase == CheckoutControllerPhase.frozen
                ? onCancel
                : null,
            icon: const Icon(Icons.close),
            label: const Text('Cancel Checkout'),
          ),
        ];

      case CheckoutControllerPhase.cancelInFlight:
        return [const _LoadingRow(label: 'Cancelling checkout…')];

      case CheckoutControllerPhase.cancelled:
        return [const _LoadingRow(label: 'Returning to cart…')];

      case CheckoutControllerPhase.paymentReady:
      case CheckoutControllerPhase.demoPaymentInFlight:
        final result = controller.checkoutResult;
        final attempt = controller.paymentAttempt;
        return [
          _CheckoutSummaryCard(result: result, totalPaise: totalPaise),
          const SizedBox(height: 8),
          if (attempt != null) _PaymentSummaryCard(attempt: attempt),
          const SizedBox(height: 12),
          if (isDemo) ...[
            _DemoPaymentBanner(),
            const SizedBox(height: 8),
            FilledButton.icon(
              key: const Key('demo_payment_button'),
              onPressed: phase == CheckoutControllerPhase.paymentReady
                  ? onDemoPayment
                  : null,
              icon: const Icon(Icons.science),
              label: phase == CheckoutControllerPhase.demoPaymentInFlight
                  ? const _LoadingLabel('Completing demo payment…')
                  : const Text('Complete Demo Payment'),
            ),
          ] else
            const Text(
              'Payment provider integration is not yet wired. Contact support.',
              textAlign: TextAlign.center,
            ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            key: const Key('refresh_payment_button'),
            onPressed: phase == CheckoutControllerPhase.paymentReady
                ? onRefresh
                : null,
            icon: const Icon(Icons.close),
            label: const Text('Refresh payment status'),
          ),
          const Text(
            'Payment has started; checkout can no longer be cancelled.',
          ),
        ];

      case CheckoutControllerPhase.completed:
        final attempt = controller.paymentAttempt;
        return [_PaymentSuccessCard(attempt: attempt)];
    }
  }
}

// ---------------------------------------------------------------------------
// Sub-widgets
// ---------------------------------------------------------------------------

class _LoadingRow extends StatelessWidget {
  const _LoadingRow({required this.label});
  final String label;

  @override
  Widget build(BuildContext context) => Row(
    mainAxisAlignment: MainAxisAlignment.center,
    children: [
      const SizedBox(
        width: 20,
        height: 20,
        child: CircularProgressIndicator(strokeWidth: 2),
      ),
      const SizedBox(width: 12),
      Text(label),
    ],
  );
}

class _LoadingLabel extends StatelessWidget {
  const _LoadingLabel(this.label);
  final String label;

  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      const SizedBox(
        width: 16,
        height: 16,
        child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
      ),
      const SizedBox(width: 8),
      Text(label),
    ],
  );
}

class _CheckoutSummaryCard extends StatelessWidget {
  const _CheckoutSummaryCard({this.result, required this.totalPaise});
  final CheckoutResult? result;
  final int totalPaise;

  @override
  Widget build(BuildContext context) {
    final displayPaise = result?.finalAmountPaise ?? totalPaise;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'Checkout Summary',
              style: TextStyle(fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 4),
            Text('Total: ${formatRupees(displayPaise)}'),
            if (result != null) ...[
              Text('Checkout ID: ${result!.checkoutId}'),
              Text('Status: ${result!.state.name.toUpperCase()}'),
            ],
          ],
        ),
      ),
    );
  }
}

class _PaymentSummaryCard extends StatelessWidget {
  const _PaymentSummaryCard({required this.attempt});
  final PaymentAttempt attempt;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'Payment Order',
              style: TextStyle(fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 4),
            // Amount comes from the backend attempt — not recalculated.
            Text('Amount: ${formatRupees(attempt.amountPaise)}'),
            Text('State: ${attempt.state.name.toUpperCase()}'),
            if (attempt.providerOrderId != null)
              Text('Order ID: ${attempt.providerOrderId}'),
          ],
        ),
      ),
    );
  }
}

class _DemoPaymentBanner extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: Colors.orange.shade50,
        border: Border.all(color: Colors.orange.shade300),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          Icon(Icons.warning_amber, color: Colors.orange.shade700, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              'DEMO MODE — This is a simulated payment. '
              'No real money will be charged.',
              style: TextStyle(color: Colors.orange.shade900, fontSize: 12),
            ),
          ),
        ],
      ),
    );
  }
}

class _PaymentSuccessCard extends StatelessWidget {
  const _PaymentSuccessCard({this.attempt});
  final PaymentAttempt? attempt;

  @override
  Widget build(BuildContext context) {
    return Card(
      color: Theme.of(context).colorScheme.primaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            Icon(
              Icons.check_circle_outline,
              size: 48,
              color: Theme.of(context).colorScheme.primary,
            ),
            const SizedBox(height: 8),
            Text(
              'Payment Successful',
              style: Theme.of(context).textTheme.titleLarge?.copyWith(
                color: Theme.of(context).colorScheme.onPrimaryContainer,
              ),
            ),
            if (attempt != null) ...[
              const SizedBox(height: 4),
              Text(
                'Amount paid: ${formatRupees(attempt!.amountPaise)}',
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onPrimaryContainer,
                ),
              ),
              if (attempt!.completedAt != null)
                Text(
                  'Completed: ${attempt!.completedAt}',
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.onPrimaryContainer,
                    fontSize: 12,
                  ),
                ),
            ],
            const SizedBox(height: 8),
            Text(
              'Payment confirmed by server',
              style: TextStyle(
                color: Theme.of(context).colorScheme.onPrimaryContainer,
                fontSize: 12,
                fontStyle: FontStyle.italic,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
