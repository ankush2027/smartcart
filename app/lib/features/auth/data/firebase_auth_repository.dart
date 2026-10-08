import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';

import '../models/auth_user.dart';
import 'auth_repository.dart';

/// Roles originate exclusively in Firebase-issued token claims.
class FirebaseAuthRepository implements AuthRepository, AuthStateSource, CustomerRegistration {
  FirebaseAuthRepository({FirebaseAuth? firebaseAuth})
    : _auth = firebaseAuth ?? FirebaseAuth.instance {
    _states = StreamController<AuthUser?>.broadcast(onListen: _start);
  }

  final FirebaseAuth _auth;
  late final StreamController<AuthUser?> _states;
  StreamSubscription<User?>? _subscription;
  AuthUser? _user;
  int _generation = 0;
  bool _operation = false;
  bool _disposed = false;

  @override
  AuthUser? get currentUser => _user;

  @override
  Stream<AuthUser?> get authStateChanges => _states.stream;

  void _start() {
    _subscription ??= _auth.idTokenChanges().listen(
      _onTokenChanged,
      onError: (Object error) {
        if (_operation || _disposed) return;
        _generation++;
        _publish(null);
        _states.addError(_failure(error));
      },
    );
  }

  Future<AuthUser> _mapUser(User user) async {
    final token = await user.getIdTokenResult();
    return AuthUser(
      uid: user.uid,
      role: token.claims?['role'] == 'manager'
          ? UserRole.manager
          : UserRole.customer,
    );
  }

  Future<void> _onTokenChanged(User? user) async {
    if (_operation || _disposed) return;
    final generation = ++_generation;
    // Remove old authorization before asynchronously resolving new claims.
    _publish(null);
    if (user == null) return;
    try {
      final mapped = await _mapUser(user);
      if (!_disposed &&
          generation == _generation &&
          _auth.currentUser?.uid == user.uid) {
        _publish(mapped);
      }
    } catch (error) {
      if (!_disposed && generation == _generation) {
        _states.addError(_failure(error));
      }
    }
  }

  void _publish(AuthUser? user) {
    if (_disposed) return;
    _user = user;
    _states.add(user);
  }

  @override
  Future<AuthUser> signIn({
    required String email,
    required String password,
  }) async {
    _operation = true;
    final generation = ++_generation;
    _publish(null);
    try {
      // A failed attempt must not leave a previous Firebase session active.
      if (_auth.currentUser != null) await _auth.signOut();
      final credential = await _auth.signInWithEmailAndPassword(
        email: email.trim(),
        password: password,
      );
      final firebaseUser = credential.user;
      if (firebaseUser == null) throw const AuthFailure();
      final user = await _mapUser(firebaseUser);
      if (_disposed ||
          generation != _generation ||
          _auth.currentUser?.uid != user.uid) {
        throw const AuthFailure();
      }
      _publish(user);
      return user;
    } catch (error) {
      _publish(null);
      // Fail closed even when token retrieval fails after credential sign-in.
      try {
        await _auth.signOut();
      } catch (_) {
        /* Keep local access cleared. */
      }
      throw _failure(error);
    } finally {
      _operation = false;
    }
  }

  @override
  Future<AuthUser> signUp({
    required String fullName,
    required String email,
    required String password,
  }) async {
    if (fullName.trim().isEmpty) {
      throw const AuthFailure('Please enter your full name.');
    }
    _operation = true;
    final generation = ++_generation;
    _publish(null);
    var accountCreated = false;
    try {
      if (_auth.currentUser != null) await _auth.signOut();
      final credential = await _auth.createUserWithEmailAndPassword(
        email: email.trim(),
        password: password,
      );
      final firebaseUser = credential.user;
      if (firebaseUser == null) throw const AuthFailure();
      accountCreated = true;
      await firebaseUser.updateDisplayName(fullName.trim());
      final user = await _mapUser(firebaseUser);
      if (_disposed || generation != _generation ||
          _auth.currentUser?.uid != user.uid) {
        throw const AuthFailure();
      }
      _publish(user);
      return user;
    } catch (error) {
      _publish(null);
      try {
        await _auth.signOut();
      } catch (_) {
        // Local authorization remains cleared even if Firebase is unreachable.
      }
      if (accountCreated) {
        throw const AuthFailure(
          'Your account was created, but setup could not finish. Please log in. '
          'Your name may not have been saved.',
        );
      }
      throw _failure(error);
    } finally {
      _operation = false;
    }
  }

  @override
  Future<void> signOut() async {
    _operation = true;
    _generation++;
    try {
      await _auth.signOut();
      _publish(null);
    } catch (_) {
      throw const AuthFailure('Unable to sign out. Please try again.');
    } finally {
      _operation = false;
    }
  }

  static AuthFailure _failure(Object error) {
    if (error is AuthFailure) return error;
    final code = error is FirebaseAuthException ? error.code : null;
    return AuthFailure(switch (code) {
      'invalid-credential' ||
      'wrong-password' ||
      'user-not-found' ||
      'invalid-login-credentials' => 'Incorrect email or password.',
      'invalid-email' => 'Please enter a valid email address.',
      'email-already-in-use' => 'Unable to create this account. Try logging in with this email.',
      'weak-password' => 'Choose a stronger password that meets the account password policy.',
      'operation-not-allowed' => 'Email registration is unavailable. Please contact support.',
      'user-token-expired' || 'invalid-user-token' => 'Your session expired. Please log in again.',
      'user-disabled' => 'This account has been disabled.',
      'network-request-failed' => 'Check your connection and try again.',
      'too-many-requests' => 'Too many attempts. Please try again later.',
      _ => 'Unable to sign in. Please try again.',
    });
  }

  void dispose() {
    _disposed = true;
    _generation++;
    _subscription?.cancel();
    _states.close();
  }
}
