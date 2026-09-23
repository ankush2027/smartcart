import 'package:flutter/foundation.dart';

import '../data/cart_repository.dart';
import '../models/customer_cart.dart';

enum CartLoadState { loading, ready, noSession, error }

class CartController extends ChangeNotifier {
  CartController(this._repository);

  final CartRepository _repository;
  CartLoadState _state = CartLoadState.loading;
  CustomerCart? _cart;
  bool _inFlight = false;
  bool _disposed = false;

  CartLoadState get state => _state;
  CustomerCart? get cart => _cart;
  bool get isDemo => _repository.isDemo;

  Future<void> load() async {
    if (_inFlight || _disposed) return;
    _inFlight = true;
    _state = CartLoadState.loading;
    _cart = null;
    notifyListeners();
    try {
      final cart = await _repository.loadCurrentCart();
      if (_disposed) return;
      _cart = cart;
      _state = cart == null ? CartLoadState.noSession : CartLoadState.ready;
    } catch (_) {
      if (!_disposed) _state = CartLoadState.error;
    } finally {
      _inFlight = false;
      if (!_disposed) notifyListeners();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
