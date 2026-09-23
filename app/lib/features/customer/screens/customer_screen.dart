import 'package:flutter/material.dart';

import '../../auth/controllers/auth_controller.dart';
import '../controllers/cart_controller.dart';
import '../data/cart_repository.dart';
import '../models/customer_cart.dart';
import '../widgets/bill_summary.dart';
import '../widgets/cart_item_card.dart';

class CustomerScreen extends StatefulWidget {
  const CustomerScreen({
    super.key,
    required this.auth,
    required this.repository,
  });
  final AuthController auth;
  final CartRepository repository;

  @override
  State<CustomerScreen> createState() => _CustomerScreenState();
}

class _CustomerScreenState extends State<CustomerScreen> {
  late final CartController _cart;

  @override
  void initState() {
    super.initState();
    _cart = CartController(widget.repository)..load();
  }

  @override
  void dispose() {
    _cart.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([_cart, widget.auth]),
    builder: (context, child) => Scaffold(
      appBar: AppBar(title: const Text('SmartCart')),
      body: SafeArea(
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 600),
            child: ListView(
              padding: const EdgeInsets.all(24),
              children: [
                Text(
                  'Customer Cart',
                  style: Theme.of(context).textTheme.headlineMedium,
                ),
                const SizedBox(height: 12),
                if (_cart.isDemo) ...[
                  const Text(
                    'Development demo • Sample cart and prices. '
                    'No live SmartCart connection.',
                  ),
                  const SizedBox(height: 24),
                ],
                ..._content(context),
                const SizedBox(height: 24),
                if (widget.auth.error != null)
                  Semantics(
                    liveRegion: true,
                    child: Text(
                      widget.auth.error!,
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.error,
                      ),
                    ),
                  ),
                OutlinedButton.icon(
                  onPressed: widget.auth.isBusy ? null : widget.auth.signOut,
                  icon: const Icon(Icons.logout),
                  label: Text(widget.auth.isBusy ? 'Signing out…' : 'Logout'),
                ),
              ],
            ),
          ),
        ),
      ),
    ),
  );

  List<Widget> _content(BuildContext context) {
    switch (_cart.state) {
      case CartLoadState.loading:
        return [
          const Center(child: CircularProgressIndicator()),
          const SizedBox(height: 16),
          const Text('Loading your cart...', textAlign: TextAlign.center),
        ];
      case CartLoadState.error:
        return [
          Semantics(
            liveRegion: true,
            child: const Text('Unable to load your cart. Please try again.'),
          ),
          const SizedBox(height: 16),
          FilledButton(onPressed: _cart.load, child: const Text('Retry')),
        ];
      case CartLoadState.noSession:
        return [
          Text('No active cart', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 8),
          const Text('There is currently no active SmartCart session.'),
        ];
      case CartLoadState.ready:
        final cart = _cart.cart!;
        return [
          Text(
            cart.status == CartStatus.active ? 'Active Cart' : 'Completed Cart',
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: 8),
          Text(cart.displayId),
          Text(
            'Status: ${cart.status == CartStatus.active ? 'Active' : 'Completed'}',
          ),
          Text('Item count: ${cart.itemCount}'),
          const SizedBox(height: 24),
          if (cart.items.isEmpty) ...[
            Text(
              'Your cart is empty.',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 8),
            const Text('Products added to your SmartCart will appear here.'),
          ] else
            ...cart.items.map((item) => CartItemCard(item: item)),
          const SizedBox(height: 16),
          BillSummary(cart: cart),
        ];
    }
  }
}
