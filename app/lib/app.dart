import 'package:flutter/material.dart';

import 'core/routes/app_routes.dart';
import 'core/theme/app_theme.dart';
import 'features/auth/controllers/auth_controller.dart';
import 'features/auth/data/auth_repository.dart';
import 'features/auth/data/firebase_auth_repository.dart';
import 'features/checkout/data/checkout_repository.dart';
import 'features/checkout/data/firebase_checkout_token_provider.dart';
import 'features/checkout/data/http_checkout_repository.dart';
import 'features/customer/data/cart_repository.dart';
import 'features/customer/data/firebase_cart_repository.dart';
import 'features/customer/services/cart_pairing_service.dart';
import 'features/manager/data/manager_repository.dart';
import 'features/manager/data/firebase_manager_repository.dart';

class SmartCartApp extends StatefulWidget {
  const SmartCartApp({
    super.key,
    this.authRepository,
    this.cartRepository,
    this.managerRepository,
    this.pairingService,
    this.checkoutRepository,
  });

  final AuthRepository? authRepository;
  final CartRepository? cartRepository;
  final ManagerRepository? managerRepository;
  final CartPairingService? pairingService;

  /// Optional checkout repository override (for tests). When null, the
  /// production HttpCheckoutRepository is used if the API base URL is set.
  final CheckoutRepository? checkoutRepository;

  @override
  State<SmartCartApp> createState() => _SmartCartAppState();
}

class _SmartCartAppState extends State<SmartCartApp> {
  late final AuthRepository _repository =
      widget.authRepository ?? FirebaseAuthRepository();
  late final AuthController _auth = AuthController(_repository);

  late final CartRepository _cart =
      widget.cartRepository ?? FirebaseCartRepository(auth: _repository);

  late final CartPairingService? _pairing =
      widget.pairingService ??
      (widget.cartRepository == null ? HttpCartPairingService() : null);

  late final CheckoutRepository? _checkout =
      widget.checkoutRepository ??
      (widget.cartRepository == null
          ? HttpCheckoutRepository(tokens: FirebaseCheckoutTokenProvider())
          : null);

  late final ManagerRepository _manager =
      widget.managerRepository ?? FirebaseManagerRepository(auth: _repository);

  @override
  void dispose() {
    _auth.dispose();
    if (widget.cartRepository == null) {
      (_cart as RealtimeCartRepository).dispose();
    }
    if (widget.managerRepository == null) {
      (_manager as RealtimeManagerRepository).dispose();
    }
    if (widget.authRepository == null) {
      (_repository as FirebaseAuthRepository).dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'SmartCart',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light,
      initialRoute: AppRoutes.authentication,
      routes: AppRoutes.routes(
        _auth,
        _cart,
        _manager,
        pairing: _pairing,
        checkoutRepository: _checkout,
      ),
    );
  }
}
