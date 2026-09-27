import 'package:flutter/material.dart';

import '../../auth/controllers/auth_controller.dart';
import '../controllers/cart_controller.dart';
import '../data/cart_repository.dart';
import '../models/customer_cart.dart';
import '../services/cart_pairing_service.dart';
import '../widgets/bill_summary.dart';
import '../widgets/cart_item_card.dart';

class CustomerScreen extends StatefulWidget {
  const CustomerScreen({
    super.key,
    required this.auth,
    required this.repository,
    this.pairing,
  });
  final AuthController auth;
  final CartRepository repository;
  final CartPairingService? pairing;

  @override
  State<CustomerScreen> createState() => _CustomerScreenState();
}

class _CustomerScreenState extends State<CustomerScreen> {
  late final CartController _cart;
  final _cartId = TextEditingController();
  final _form = GlobalKey<FormState>();

  @override
  void initState() {
    super.initState();
    _cart = CartController(widget.repository, pairing: widget.pairing)..load();
  }

  @override
  void dispose() {
    _cart.dispose();
    _cartId.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    return ListenableBuilder(
      listenable: Listenable.merge([_cart, widget.auth]),
      builder: (context, child) => Scaffold(
        appBar: AppBar(title: const Text('SmartCart')),
        body: SafeArea(
          child: Align(
            alignment: Alignment.topCenter,
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 640),
              child: ListView(
                padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 24),
                children: [
                  Text(
                    'Customer Cart',
                    style: theme.textTheme.headlineMedium?.copyWith(
                      fontWeight: FontWeight.w800,
                      letterSpacing: -0.5,
                    ),
                  ),
                  const SizedBox(height: 12),
                  if (_cart.isDemo) ...[
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: const Color(0xFFFFF8E1),
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(color: const Color(0xFFFFE082)),
                      ),
                      child: Row(
                        children: [
                          const Icon(
                            Icons.info_outline_rounded,
                            size: 18,
                            color: Color(0xFFF57F17),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Text(
                              'Development demo • Sample cart and prices. '
                              'No live SmartCart connection.',
                              style: theme.textTheme.bodySmall?.copyWith(
                                color: const Color(0xFF5D4037),
                                fontWeight: FontWeight.w500,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 20),
                  ],
                  ..._content(context),
                  const SizedBox(height: 28),
                  if (widget.auth.error != null) ...[
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                      margin: const EdgeInsets.only(bottom: 16),
                      decoration: BoxDecoration(
                        color: colorScheme.error.withValues(alpha: 0.08),
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(
                          color: colorScheme.error.withValues(alpha: 0.2),
                        ),
                      ),
                      child: Row(
                        children: [
                          Icon(
                            Icons.error_outline_rounded,
                            size: 18,
                            color: colorScheme.error,
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: Semantics(
                              liveRegion: true,
                              child: Text(
                                widget.auth.error!,
                                style: TextStyle(
                                  color: colorScheme.error,
                                  fontWeight: FontWeight.w500,
                                ),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                  OutlinedButton.icon(
                    onPressed: widget.auth.isBusy ? null : widget.auth.signOut,
                    style: OutlinedButton.styleFrom(
                      padding: const EdgeInsets.symmetric(vertical: 14),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                    ),
                    icon: const Icon(Icons.logout, size: 18),
                    label: Text(widget.auth.isBusy ? 'Signing out…' : 'Logout'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  List<Widget> _content(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    switch (_cart.state) {
      case CartLoadState.loading:
        return [
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 40),
            child: Column(
              children: [
                Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: colorScheme.primaryContainer.withValues(alpha: 0.5),
                    shape: BoxShape.circle,
                  ),
                  child: const CircularProgressIndicator(strokeWidth: 3),
                ),
                const SizedBox(height: 20),
                const Text(
                  'Loading your cart...',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 15, fontWeight: FontWeight.w500),
                ),
              ],
            ),
          ),
        ];
      case CartLoadState.syncError:
      case CartLoadState.error:
        return [
          Container(
            padding: const EdgeInsets.all(24),
            decoration: BoxDecoration(
              color: colorScheme.surface,
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: colorScheme.error.withValues(alpha: 0.3)),
            ),
            child: Column(
              children: [
                Icon(Icons.cloud_off_rounded, size: 40, color: colorScheme.error),
                const SizedBox(height: 12),
                Semantics(
                  liveRegion: true,
                  child: Text(
                    _cart.error ?? 'Unable to load your cart. Please try again.',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color: colorScheme.error,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
                const SizedBox(height: 18),
                FilledButton(
                  onPressed: _cart.load,
                  child: const Text('Retry'),
                ),
              ],
            ),
          ),
        ];
      case CartLoadState.connecting:
        return [
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 40),
            child: Column(
              children: [
                Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: colorScheme.primaryContainer.withValues(alpha: 0.5),
                    shape: BoxShape.circle,
                  ),
                  child: const CircularProgressIndicator(strokeWidth: 3),
                ),
                const SizedBox(height: 20),
                const Text(
                  'Connecting to your live cart...',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 15, fontWeight: FontWeight.w500),
                ),
              ],
            ),
          ),
        ];
      case CartLoadState.pairing:
      case CartLoadState.noSession:
        return [
          Card(
            elevation: 0,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(20),
              side: BorderSide(
                color: colorScheme.outline.withValues(alpha: 0.8),
                width: 1.2,
              ),
            ),
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Center(
                    child: Container(
                      width: 56,
                      height: 56,
                      decoration: BoxDecoration(
                        color: colorScheme.primaryContainer,
                        borderRadius: BorderRadius.circular(16),
                      ),
                      child: Icon(
                        Icons.qr_code_scanner_rounded,
                        size: 28,
                        color: colorScheme.primary,
                      ),
                    ),
                  ),
                  const SizedBox(height: 16),
                  Text(
                    'No active cart',
                    textAlign: TextAlign.center,
                    style: theme.textTheme.titleLarge?.copyWith(
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(height: 8),
                  const Text(
                    'There is currently no active SmartCart session.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Color(0xFF5F6E67)),
                  ),
                  if (_cart.canPair) ...[
                    const SizedBox(height: 20),
                    const Divider(),
                    const SizedBox(height: 16),
                    Form(
                      key: _form,
                      child: TextFormField(
                        controller: _cartId,
                        enabled: !_cart.pairingBusy,
                        decoration: const InputDecoration(
                          labelText: 'Enter Cart ID',
                          prefixIcon: Icon(Icons.tag_rounded, size: 20),
                          border: OutlineInputBorder(),
                        ),
                        autocorrect: false,
                        validator: (value) =>
                            value == null || value.trim().isEmpty
                                ? 'Enter a Cart ID.'
                                : null,
                      ),
                    ),
                    if (_cart.error != null) ...[
                      const SizedBox(height: 12),
                      Semantics(
                        liveRegion: true,
                        child: Text(
                          _cart.error!,
                          style: TextStyle(
                            color: colorScheme.error,
                            fontSize: 13,
                          ),
                        ),
                      ),
                    ],
                    const SizedBox(height: 16),
                    FilledButton(
                      onPressed: _cart.pairingBusy
                          ? null
                          : () {
                              if (_form.currentState!.validate()) {
                                FocusScope.of(context).unfocus();
                                _cart.pair(_cartId.text);
                              }
                            },
                      style: FilledButton.styleFrom(
                        padding: const EdgeInsets.symmetric(vertical: 14),
                      ),
                      child: _cart.pairingBusy
                          ? const SizedBox(
                              width: 20,
                              height: 20,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                valueColor: AlwaysStoppedAnimation<Color>(
                                  Colors.white,
                                ),
                                semanticsLabel: 'Connecting cart',
                              ),
                            )
                          : const Text('Connect Cart'),
                    ),
                  ],
                ],
              ),
            ),
          ),
        ];
      case CartLoadState.ready:
        final cart = _cart.cart!;
        final isActive = cart.status == CartStatus.active;

        return [
          Card(
            elevation: 0,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(18),
              side: BorderSide(
                color: colorScheme.outline.withValues(alpha: 0.8),
                width: 1.2,
              ),
            ),
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Wrap(
                    alignment: WrapAlignment.spaceBetween,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      Text(
                        isActive ? 'Active Cart' : 'Completed Cart',
                        style: theme.textTheme.titleLarge?.copyWith(
                          fontWeight: FontWeight.w800,
                          letterSpacing: -0.3,
                        ),
                      ),
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 10,
                          vertical: 4,
                        ),
                        decoration: BoxDecoration(
                          color: isActive
                              ? const Color(0xFFE8F5E9)
                              : colorScheme.surfaceContainerHighest,
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Text(
                          'Status: ${isActive ? 'Active' : 'Completed'}',
                          style: TextStyle(
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                            color: isActive
                                ? const Color(0xFF2E7D32)
                                : colorScheme.onSurfaceVariant,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 10),
                  Wrap(
                    crossAxisAlignment: WrapCrossAlignment.center,
                    spacing: 8,
                    runSpacing: 6,
                    children: [
                      Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(
                            Icons.fingerprint_rounded,
                            size: 16,
                            color: colorScheme.onSurfaceVariant,
                          ),
                          const SizedBox(width: 6),
                          Text(
                            cart.displayId,
                            style: theme.textTheme.bodyMedium?.copyWith(
                              color: colorScheme.onSurfaceVariant,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        ],
                      ),
                      if (cart.cartId != null)
                        Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 8,
                            vertical: 2,
                          ),
                          decoration: BoxDecoration(
                            color: colorScheme.primaryContainer.withValues(alpha: 0.5),
                            borderRadius: BorderRadius.circular(6),
                          ),
                          child: Text(
                            'Cart ID: ${cart.cartId}',
                            style: TextStyle(
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                              color: colorScheme.primary,
                            ),
                          ),
                        ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Icon(
                        Icons.shopping_bag_outlined,
                        size: 16,
                        color: colorScheme.onSurfaceVariant,
                      ),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          'Item count: ${cart.itemCount}',
                          style: theme.textTheme.bodyMedium?.copyWith(
                            color: colorScheme.onSurfaceVariant,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 20),
          if (cart.items.isEmpty) ...[
            Card(
              elevation: 0,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(16),
                side: BorderSide(
                  color: colorScheme.outline.withValues(alpha: 0.7),
                  width: 1,
                ),
              ),
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 36, horizontal: 20),
                child: Column(
                  children: [
                    Container(
                      width: 56,
                      height: 56,
                      decoration: BoxDecoration(
                        color: colorScheme.surfaceContainerHighest.withValues(alpha: 0.5),
                        shape: BoxShape.circle,
                      ),
                      child: Icon(
                        Icons.shopping_cart_outlined,
                        size: 28,
                        color: colorScheme.onSurfaceVariant,
                      ),
                    ),
                    const SizedBox(height: 14),
                    Text(
                      'Your cart is empty.',
                      style: theme.textTheme.titleMedium?.copyWith(
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 6),
                    Text(
                      'Products added to your SmartCart will appear here.',
                      textAlign: TextAlign.center,
                      style: theme.textTheme.bodyMedium?.copyWith(
                        color: colorScheme.onSurfaceVariant,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ] else ...[
            Padding(
              padding: const EdgeInsets.only(bottom: 12, left: 4),
              child: Text(
                'Cart Items',
                style: theme.textTheme.titleSmall?.copyWith(
                  fontWeight: FontWeight.w700,
                  color: colorScheme.onSurfaceVariant,
                  letterSpacing: 0.5,
                ),
              ),
            ),
            ...cart.items.map((item) => CartItemCard(item: item)),
          ],
          const SizedBox(height: 16),
          BillSummary(cart: cart),
        ];
    }
  }
}
