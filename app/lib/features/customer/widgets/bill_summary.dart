import 'package:flutter/material.dart';

import '../models/customer_cart.dart';

class BillSummary extends StatelessWidget {
  const BillSummary({super.key, required this.cart});
  final CustomerCart cart;

  @override
  Widget build(BuildContext context) => Card.filled(
    margin: EdgeInsets.zero,
    child: Padding(
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Current Bill', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 16),
          Text('Items: ${cart.itemCount}'),
          const SizedBox(height: 8),
          Text('Subtotal: ${formatRupees(cart.subtotalPaise)}'),
          const Divider(height: 32),
          Text(
            'Total: ${formatRupees(cart.totalPaise)}',
            style: Theme.of(context).textTheme.titleLarge,
          ),
        ],
      ),
    ),
  );
}
