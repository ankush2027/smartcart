import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../../core/data/repository_failure.dart';
import '../data/cart_repository.dart';
import '../models/customer_cart.dart';
import '../services/cart_pairing_service.dart';

enum CartLoadState {
  loading,
  ready,
  noSession,
  error,
  pairing,
  connecting,
  syncError,
}

class CartController extends ChangeNotifier {
  CartController(
    this._repository, {
    this.pairing,
    this.syncWindow = const Duration(seconds: 20),
  });
  final CartRepository _repository;
  final CartPairingService? pairing;
  final Duration syncWindow;
  CartLoadState _state = CartLoadState.loading;
  CustomerCart? _cart;
  bool _inFlight = false;
  bool _pairInFlight = false;
  bool get pairingBusy => _pairInFlight;
  bool _disposed = false;
  int _generation = 0;
  Timer? _timer;
  StreamSubscription<CustomerCart?>? _subscription;
  String? _error;
  CartLoadState get state => _state;
  CustomerCart? get cart => _cart;
  String? get error => _error;
  bool get isDemo => _repository.isDemo;
  bool get canPair => pairing != null;

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  void _accept(CustomerCart? cart) {
    _cart = cart;
    if (cart != null) {
      _timer?.cancel();
      _error = null;
      _state = CartLoadState.ready;
    } else if (_state != CartLoadState.pairing &&
        _state != CartLoadState.connecting) {
      _timer?.cancel();
      _state = CartLoadState.noSession;
    }
    _notify();
  }

  void _fail(Object error) {
    _timer?.cancel();
    _cart = null;
    _state = CartLoadState.error;
    _error = error is RepositoryFailure
        ? error.message
        : 'Unable to load your cart. Please try again.';
    _notify();
  }

  Future<void> load() async {
    if (_inFlight || _disposed || _state == CartLoadState.pairing) return;
    final generation = ++_generation;
    _inFlight = true;
    _timer?.cancel();
    _state = CartLoadState.loading;
    _cart = null;
    _error = null;
    _notify();
    try {
      await _subscription?.cancel();
      _subscription = null;
      if (_disposed || generation != _generation) return;
      final repository = _repository;
      if (repository is RealtimeCartRepository) {
        _subscription = repository.watchCurrentCart().listen(
          (cart) {
            if (!_disposed && generation == _generation) _accept(cart);
          },
          onError: (Object error) {
            if (!_disposed && generation == _generation) _fail(error);
          },
          onDone: () {
            if (!_disposed && generation == _generation) {
              _fail(StateError('closed'));
            }
          },
        );
        _timer = Timer(syncWindow, () {
          if (!_disposed &&
              generation == _generation &&
              _state == CartLoadState.loading) {
            _fail(const RepositoryFailure(RepositoryFailureCode.unavailable));
          }
        });
      } else {
        final cart = await repository.loadCurrentCart();
        if (!_disposed && generation == _generation) _accept(cart);
      }
    } catch (error) {
      if (!_disposed && generation == _generation) _fail(error);
    } finally {
      _inFlight = false;
    }
  }

  Future<void> pair(String cartId) async {
    final service = pairing;
    if (_disposed ||
        _pairInFlight ||
        service == null ||
        _state != CartLoadState.noSession) {
      return;
    }
    final generation = _generation;
    _pairInFlight = true;
    _state = CartLoadState.pairing;
    _error = null;
    _notify();
    try {
      await service.pair(cartId);
      if (_disposed ||
          generation != _generation ||
          _cart != null ||
          _state == CartLoadState.error) {
        return;
      }
      // Keep the one existing RTDB query. HTTP success is not a cart snapshot.
      _state = CartLoadState.connecting;
      _notify();
      _timer?.cancel();
      _timer = Timer(syncWindow, () {
        if (_disposed ||
            generation != _generation ||
            _cart != null ||
            _state == CartLoadState.error) {
          return;
        }
        _state = CartLoadState.syncError;
        _error = 'Your cart assignment may have succeeded, but live data has not arrived. Retry discovery; if it continues, ask the cart operator to check synchronization.';
        _notify();
      });
    } catch (error) {
      if (_disposed ||
          generation != _generation ||
          _cart != null ||
          _state == CartLoadState.error) {
        return;
      }
      _state = CartLoadState.noSession;
      _error = error is PairingFailure
          ? error.message
          : 'Unable to connect the cart. Please try again.';
      _notify();
    } finally {
      _pairInFlight = false;
      _notify();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _generation++;
    _timer?.cancel();
    _subscription?.cancel();
    _cart = null;
    _error = null;
    super.dispose();
  }
}
