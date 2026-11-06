import 'package:flutter/material.dart';

import '../../auth/controllers/auth_controller.dart';
import '../../checkout/controllers/checkout_controller.dart';
import '../../checkout/data/checkout_repository.dart';
import '../../checkout/widgets/checkout_panel.dart';
import '../controllers/cart_controller.dart';
import '../data/cart_repository.dart';
import '../models/customer_cart.dart';
import '../services/cart_pairing_service.dart';
import '../widgets/bill_summary.dart';
import '../widgets/cart_item_card.dart';
import '../widgets/cart_qr_scanner.dart';

class CustomerScreen extends StatefulWidget {
  const CustomerScreen({
    super.key,
    required this.auth,
    required this.repository,
    this.pairing,
    this.checkoutRepository,
    this.demoPaymentEnabled = const bool.fromEnvironment(
      'SMARTCART_DEMO_PAYMENT',
    ),
    this.cameraBuilder,
  });
  final AuthController auth;
  final CartRepository repository;
  final CartPairingService? pairing;

  /// Optional checkout repository. When null, checkout is unavailable.
  final CheckoutRepository? checkoutRepository;
  // Explicit payment demo opt-in, independent of live Firebase cart discovery.
  final bool demoPaymentEnabled;
  final CartCameraBuilder? cameraBuilder;

  @override
  State<CustomerScreen> createState() => _CustomerScreenState();
}

class _CustomerScreenState extends State<CustomerScreen>
    with WidgetsBindingObserver {
  late final CartController _cart;
  CheckoutController? _checkout;
  final _cartId = TextEditingController();
  final _form = GlobalKey<FormState>();
  String? _sessionId;
  CustomerCart? _lastCart;
  bool _scanning = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _cart = CartController(widget.repository, pairing: widget.pairing)..load();
    final repo = widget.checkoutRepository;
    if (repo != null) {
      _checkout = CheckoutController(repo);
    }
    _cart.addListener(_sessionChanged);
  }

  void _sessionChanged() {
    final id = _cart.cart?.displayId;
    if (_cart.cart != null) _lastCart = _cart.cart;
    if (id != null) _scanning = false;
    if (id != null && id != _sessionId) {
      _sessionId = id;
      _checkout?.reset();
      final sid = int.tryParse(id);
      if (sid != null) _checkout?.recover(sid);
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final sid = int.tryParse(_sessionId ?? '');
    if (state == AppLifecycleState.resumed && sid != null) {
      _checkout?.recover(sid);
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _cart.removeListener(_sessionChanged);
    _cart.dispose();
    _checkout?.dispose();
    _cartId.dispose();
    super.dispose();
  }

  String _idempotencyKey() =>
      'flutter-${DateTime.now().microsecondsSinceEpoch}';

  @override
  Widget build(BuildContext context) {
    final listenables = <Listenable>[_cart, widget.auth];
    final checkout = _checkout;
    if (checkout != null) listenables.add(checkout);

    return ListenableBuilder(
      listenable: Listenable.merge(listenables),
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
                  if (widget.demoPaymentEnabled) ...[
                    const Text('Software demonstration • Product events are simulated by the operator. Payments are simulated; no money is charged.'),
                    const SizedBox(height: 16),
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
  }

  List<Widget> _content(BuildContext context) {
    if (_scanning && _cart.state == CartLoadState.noSession) {
      return [
        CartQrScanner(
          cameraBuilder: widget.cameraBuilder,
          onManual: () => setState(() => _scanning = false),
          onCart: (id) {
            if (!mounted ||
                widget.auth.user == null ||
                _cart.state != CartLoadState.noSession) {
              return;
            }
            setState(() => _scanning = false);
            _cartId.text = id;
            _cart.pair(id);
          },
        ),
      ];
    }
    final receipt = _checkout;
    final sid = int.tryParse(_sessionId ?? '');
    if (receipt != null &&
        sid != null &&
        receipt.phase != CheckoutControllerPhase.shopping) {
      final result = receipt.checkoutResult;
      return [
        Text('Checkout', style: Theme.of(context).textTheme.headlineSmall),
        if (receipt.items.isNotEmpty)
          ...receipt.items.map(
            (item) => ListTile(
              title: Text('${item['name']}'),
              subtitle: Text('Quantity: ${item['quantity']}'),
              trailing: Text(
                formatRupees((item['total_paise'] as num).toInt()),
              ),
            ),
          )
        else
          ...?_lastCart?.items.map((item) => CartItemCard(item: item)),
        CheckoutPanel(
          controller: receipt,
          sessionId: sid,
          totalPaise: result?.finalAmountPaise ?? _lastCart?.totalPaise ?? 0,
          isDemo: widget.demoPaymentEnabled,
          onRefresh: () => receipt.recover(sid),
          onCancel: result == null
              ? null
              : () => receipt.cancelCheckout(
                  sessionId: sid,
                  checkoutId: result.checkoutId,
                ),
          onStartPayment: result == null
              ? null
              : () => receipt.startPayment(
                  sessionId: sid,
                  checkoutId: result.checkoutId,
                  idempotencyKey: _idempotencyKey(),
                ),
          onDemoPayment: result == null
              ? null
              : () => receipt.simulateDemoPayment(
                  sessionId: sid,
                  checkoutId: result.checkoutId,
                ),
        ),
        if (receipt.phase == CheckoutControllerPhase.completed)
          TextButton(
            onPressed: () {
              _sessionId = null;
              _lastCart = null;
              receipt.reset();
              _cart.load();
            },
            child: const Text('Continue to cart pairing'),
          ),
      ];
    }
    switch (_cart.state) {
      case CartLoadState.loading:
        return [
          const Center(child: CircularProgressIndicator()),
          const SizedBox(height: 16),
          const Text('Loading your cart...', textAlign: TextAlign.center),
        ];
      case CartLoadState.syncError:
      case CartLoadState.error:
        return [
          Semantics(
            liveRegion: true,
            child: Text(
              _cart.error ?? 'Unable to load your cart. Please try again.',
            ),
          ),
          const SizedBox(height: 16),
          FilledButton(onPressed: _cart.load, child: const Text('Retry')),
        ];
      case CartLoadState.connecting:
        return [
          const Center(child: CircularProgressIndicator()),
          const Text(
            'Connecting to your live cart...',
            textAlign: TextAlign.center,
          ),
        ];
      case CartLoadState.pairing:
      case CartLoadState.noSession:
        return [
          Text('No active cart', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 8),
          const Text('There is currently no active SmartCart session.'),
          if (_cart.canPair) ...[
            const SizedBox(height: 16),
            FilledButton.icon(
              key: const Key('scan_cart'),
              onPressed: _cart.pairingBusy
                  ? null
                  : () => setState(() => _scanning = true),
              icon: const Icon(Icons.qr_code_scanner),
              label: const Text('Scan Cart QR Code'),
            ),
            const SizedBox(height: 12),
            const Text('Or enter the Cart ID printed on the cart.'),
            Form(
              key: _form,
              child: TextFormField(
                controller: _cartId,
                enabled: !_cart.pairingBusy,
                decoration: const InputDecoration(
                  labelText: 'Enter Cart ID',
                  border: OutlineInputBorder(),
                ),
                autocorrect: false,
                validator: (value) => value == null || value.trim().isEmpty
                    ? 'Enter a Cart ID.'
                    : null,
              ),
            ),
            if (_cart.error != null) ...[
              const SizedBox(height: 12),
              Semantics(liveRegion: true, child: Text(_cart.error!)),
            ],
            const SizedBox(height: 16),
            FilledButton(
              key: const Key('connect_cart'),
              onPressed: _cart.pairingBusy
                  ? null
                  : () {
                      if (_form.currentState!.validate()) {
                        FocusScope.of(context).unfocus();
                        _cart.pair(_cartId.text);
                      }
                    },
              child: _cart.pairingBusy
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(
                        strokeWidth: 2,
                        semanticsLabel: 'Connecting cart',
                      ),
                    )
                  : const Text('Connect Cart'),
            ),
          ],
        ];
      case CartLoadState.ready:
        final cart = _cart.cart!;
        final checkout = _checkout;

        // Derive session ID from displayId when it is a numeric session key.
        final sessionId = int.tryParse(cart.displayId);

        return [
          Text(
            cart.status == CartStatus.active ? 'Active Cart' : 'Completed Cart',
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: 8),
          Text(cart.displayId),
          if (cart.cartId != null) Text('Cart ID: ${cart.cartId}'),
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

          // Checkout panel — only when active, checkout repo is configured,
          // and session ID is available from the RTDB path.
          if (cart.status == CartStatus.active &&
              checkout != null &&
              sessionId != null) ...[
            const SizedBox(height: 16),
            const Divider(),
            const SizedBox(height: 8),
            CheckoutPanel(
              controller: checkout,
              sessionId: sessionId,
              totalPaise: cart.totalPaise,
              isDemo: widget.demoPaymentEnabled,
              onCheckout: () => checkout.beginCheckout(
                sessionId: sessionId,
                idempotencyKey: _idempotencyKey(),
              ),
              onCancel: () async {
                final cid = checkout.checkoutResult?.checkoutId;
                if (cid != null) {
                  await checkout.cancelCheckout(
                    sessionId: sessionId,
                    checkoutId: cid,
                  );
                }
              },
              onStartPayment: () async {
                final cid = checkout.checkoutResult?.checkoutId;
                if (cid != null) {
                  await checkout.startPayment(
                    sessionId: sessionId,
                    checkoutId: cid,
                    idempotencyKey: _idempotencyKey(),
                  );
                }
              },
              onDemoPayment: () async {
                final cid = checkout.checkoutResult?.checkoutId;
                if (cid != null) {
                  await checkout.simulateDemoPayment(
                    sessionId: sessionId,
                    checkoutId: cid,
                  );
                }
              },
            ),
          ],
        ];
    }
  }
}
