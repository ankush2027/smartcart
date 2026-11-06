import 'dart:async';

import '../../auth/data/auth_repository.dart';
import '../../auth/models/auth_user.dart';
import '../../../core/data/firebase_session_reader.dart';
import '../../../core/data/repository_failure.dart';
import '../../../core/data/session_mapper.dart';
import '../models/customer_cart.dart';
import 'cart_repository.dart';

/// Own-session discovery only. No pairing, writes, or unrestricted reads.
class FirebaseCartRepository implements RealtimeCartRepository {
  FirebaseCartRepository({required AuthRepository auth, this.reader})
    : _auth = auth {
    if (auth is! AuthStateSource) {
      throw ArgumentError('A live authentication state source is required.');
    }
    _states = auth as AuthStateSource;
    _stream = Stream<_CartValue>.multi(_listen, isBroadcast: true)
        .where(
          (event) =>
              event.generation == _generation &&
              _same(_identity, _auth.currentUser),
        )
        .map((event) {
          if (event.failure != null) throw event.failure!;
          return event.cart;
        });
  }

  final AuthRepository _auth;
  final SessionReader? reader;
  late final AuthStateSource _states;
  late final Stream<CustomerCart?> _stream;
  final _listeners = <MultiStreamController<_CartValue>>{};
  StreamSubscription<AuthUser?>? _authSubscription;
  StreamSubscription<Object?>? _dataSubscription;
  Future<void> _cancellation = Future.value();
  AuthUser? _identity;
  CustomerCart? _latest;
  bool _hasLatest = false;
  bool _disposed = false;
  int _generation = 0;

  bool _same(AuthUser? a, AuthUser? b) =>
      a?.uid == b?.uid && a?.role == b?.role;

  @override
  bool get isDemo => false;
  @override
  Stream<CustomerCart?> watchCurrentCart() => _stream;
  @override
  Future<CustomerCart?> loadCurrentCart() async {
    try {
      return await _stream.first;
    } on RepositoryFailure {
      rethrow;
    } catch (_) {
      throw RepositoryFailure(
        _disposed
            ? RepositoryFailureCode.disposed
            : RepositoryFailureCode.unavailable,
      );
    }
  }

  void _listen(MultiStreamController<_CartValue> listener) {
    if (_disposed) {
      listener.addError(
        const RepositoryFailure(RepositoryFailureCode.disposed),
      );
      listener.close();
      return;
    }
    _listeners.add(listener);
    listener.onCancel = () async {
      _listeners.remove(listener);
      if (_listeners.isEmpty) await _stop();
    };
    if (_listeners.length == 1) {
      _authSubscription = _states.authStateChanges.listen(
        (user) {
          if (!_same(user, _identity)) unawaited(_switch(user));
        },
        onError: (Object _) {
          unawaited(_switch(null));
        },
      );
      unawaited(_switch(_auth.currentUser, clear: false));
    } else if (_hasLatest && _same(_identity, _auth.currentUser)) {
      listener.add(_CartValue(_generation, _latest));
    }
  }

  Future<void> _cancelData() {
    final subscription = _dataSubscription;
    _dataSubscription = null;
    // Each switch waits for all prior cancellations before opening a new read.
    _cancellation = Future.wait<void>([
      _cancellation,
      if (subscription != null) subscription.cancel(),
    ]).then((_) {});
    return _cancellation;
  }

  Future<void> _switch(AuthUser? user, {bool clear = true}) async {
    final generation = ++_generation;
    _identity = user;
    _latest = null;
    _hasLatest = false;
    if (clear) _publish(null);
    await _cancelData();
    if (_disposed || generation != _generation || _listeners.isEmpty) return;
    if (user == null) {
      if (!clear) {
        _error(const RepositoryFailure(RepositoryFailureCode.unauthenticated));
      }
      return;
    }
    if (user.role != UserRole.customer) {
      _error(const RepositoryFailure(RepositoryFailureCode.permissionDenied));
      return;
    }
    try {
      _dataSubscription = (reader ?? FirebaseSessionReader())
          .watchOwnedSessions(user.uid)
          .listen(
            (value) {
              if (generation != _generation) return;
              if (!_same(user, _auth.currentUser)) {
                unawaited(_switch(_auth.currentUser));
                return;
              }
              try {
                CustomerCart? active;
                for (final entry in children(value).entries) {
                  validateSessionKey(entry.key);
                  final cart = parseSession(entry.key, entry.value).cart;
                  if (cart.customerUid != user.uid) {
                    throw const RepositoryFailure(
                      RepositoryFailureCode.permissionDenied,
                    );
                  }
                  if (cart.status == CartStatus.active) {
                    if (active != null) {
                      throw const RepositoryFailure(
                        RepositoryFailureCode.multipleActiveSessions,
                      );
                    }
                    active = cart;
                  }
                }
                _publish(active);
              } catch (error) {
                _error(error);
              }
            },
            onError: (Object error) {
              if (generation == _generation) _error(error);
            },
          );
    } catch (error) {
      _error(error);
    }
  }

  void _publish(CustomerCart? cart) {
    _latest = cart;
    _hasLatest = true;
    for (final listener in _listeners.toList()) {
      listener.add(_CartValue(_generation, cart));
    }
  }

  void _error(Object error) {
    _latest = null;
    _hasLatest = false;
    final safe = error is RepositoryFailure
        ? error
        : const RepositoryFailure(RepositoryFailureCode.unavailable);
    for (final listener in _listeners.toList()) {
      listener.add(_CartValue(_generation, null, safe));
    }
  }

  Future<void> _stop() async {
    _generation++;
    _latest = null;
    _hasLatest = false;
    final auth = _authSubscription;
    _authSubscription = null;
    await Future.wait([_cancelData(), if (auth != null) auth.cancel()]);
  }

  @override
  Future<void> dispose() async {
    _disposed = true;
    for (final listener in _listeners.toList()) {
      listener.close();
    }
    _listeners.clear();
    await _stop();
  }
}

class _CartValue {
  const _CartValue(this.generation, this.cart, [this.failure]);
  final RepositoryFailure? failure;
  final int generation;
  final CustomerCart? cart;
}
