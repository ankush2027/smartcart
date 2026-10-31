import 'dart:async';

import '../../features/auth/data/auth_repository.dart';
import '../../features/auth/models/auth_user.dart';
import 'repository_failure.dart';

/// One shared read bound to the authenticated identity present at construction.
/// Last-listener cancellation releases both subscriptions; values are never
/// cached across idle periods. A changed identity permanently invalidates it.
class ScopedRealtimeRead<T> {
  ScopedRealtimeRead({
    required AuthRepository auth,
    required this.role,
    required this.source,
    required this.map,
  }) : _auth = auth,
       _identity = auth.currentUser {
    if (auth is! AuthStateSource) {
      throw ArgumentError('A live authentication state source is required.');
    }
    _states = auth as AuthStateSource;
    stream = Stream<T>.multi(_listen, isBroadcast: true).map((value) {
      // Recheck at delivery, including events queued before logout/role loss.
      final failure = _terminal ?? _accessFailure();
      if (failure != null) throw failure;
      return value;
    });
  }

  final AuthRepository _auth;
  late final AuthStateSource _states;
  final AuthUser? _identity;
  final UserRole role;
  final Stream<Object?> Function() source;
  final T Function(Object?, AuthUser) map;
  late final Stream<T> stream;
  final _listeners = <MultiStreamController<T>>{};
  StreamSubscription<Object?>? _dataSubscription;
  StreamSubscription<AuthUser?>? _authSubscription;
  RepositoryFailure? _terminal;
  T? _latest;
  bool _hasLatest = false;
  int _generation = 0;

  RepositoryFailure? _accessFailure() {
    final current = _auth.currentUser;
    if (_identity == null) {
      return const RepositoryFailure(RepositoryFailureCode.unauthenticated);
    }
    if (current == null ||
        current.uid != _identity.uid ||
        current.role != _identity.role) {
      return const RepositoryFailure(RepositoryFailureCode.identityChanged);
    }
    if (current.role != role) {
      return const RepositoryFailure(RepositoryFailureCode.permissionDenied);
    }
    return null;
  }

  Future<T> first() async {
    try {
      return await stream.first;
    } on RepositoryFailure {
      rethrow;
    } catch (_) {
      throw _terminal ??
          const RepositoryFailure(RepositoryFailureCode.unavailable);
    }
  }

  void _listen(MultiStreamController<T> listener) {
    final failure = _terminal ?? _accessFailure();
    if (failure != null) {
      listener.addError(failure);
      listener.close();
      return;
    }
    _listeners.add(listener);
    listener.onCancel = () async {
      _listeners.remove(listener);
      if (_listeners.isEmpty) await _stop();
    };
    if (_hasLatest) listener.add(_latest as T);
    if (_listeners.length == 1) _start();
  }

  void _start() {
    final generation = ++_generation;
    _authSubscription = _states.authStateChanges.listen(
      (user) {
        if (generation != _generation) return;
        if (user == null ||
            user.uid != _identity!.uid ||
            user.role != _identity.role) {
          _fail(const RepositoryFailure(RepositoryFailureCode.identityChanged));
        }
      },
      onError: (Object _) {
        if (generation == _generation) {
          _fail(const RepositoryFailure(RepositoryFailureCode.unauthenticated));
        }
      },
    );
    try {
      _dataSubscription = source().listen(
        (value) {
          if (generation != _generation) return;
          final failure = _accessFailure();
          if (failure != null) {
            _fail(failure);
            return;
          }
          try {
            final mapped = map(value, _identity!);
            _latest = mapped;
            _hasLatest = true;
            for (final listener in _listeners.toList()) {
              listener.add(mapped);
            }
          } catch (error) {
            _fail(_safe(error));
          }
        },
        onError: (Object error) {
          if (generation == _generation) _fail(_safe(error));
        },
        onDone: () {
          if (generation == _generation) {
            _fail(const RepositoryFailure(RepositoryFailureCode.unavailable));
          }
        },
      );
    } catch (error) {
      _fail(_safe(error));
    }
  }

  RepositoryFailure _safe(Object error) => error is RepositoryFailure
      ? error
      : const RepositoryFailure(RepositoryFailureCode.unavailable);

  void _fail(RepositoryFailure failure) {
    _terminal = failure;
    for (final listener in _listeners.toList()) {
      listener.addError(failure);
      listener.close();
    }
    _listeners.clear();
    unawaited(_stop());
  }

  Future<void> _stop() async {
    _generation++;
    _latest = null;
    _hasLatest = false;
    final data = _dataSubscription;
    final auth = _authSubscription;
    _dataSubscription = null;
    _authSubscription = null;
    await Future.wait([
      if (data != null) data.cancel(),
      if (auth != null) auth.cancel(),
    ]);
  }

  Future<void> dispose() async {
    _terminal = const RepositoryFailure(RepositoryFailureCode.disposed);
    for (final listener in _listeners.toList()) {
      listener.close();
    }
    _listeners.clear();
    await _stop();
  }
}
