import 'package:flutter/material.dart';

import '../models/customer_cart.dart';

class CartItemCard extends StatelessWidget {
  const CartItemCard({super.key, required this.item});
  final CartItem item;

  @override
  Widget build(BuildContext context) => Card(
    margin: const EdgeInsets.only(bottom: 12),
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            item.product.name,
            style: Theme.of(context).textTheme.titleMedium,
          ),
          const SizedBox(height: 8),
          Text(
            'Qty: ${item.quantity} · ${formatRupees(item.unitPricePaise)} each',
          ),
          const SizedBox(height: 8),
          Text(
            'Line total: ${formatRupees(item.lineTotalPaise)}',
            style: Theme.of(context).textTheme.titleSmall,
          ),
        ],
      ),
    ),
  );
}
