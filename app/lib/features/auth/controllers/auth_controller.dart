import 'dart:async';

import 'package:flutter/foundation.dart';

import '../data/auth_repository.dart';
import '../models/auth_user.dart';

class AuthController extends ChangeNotifier {
  AuthController(this._repository) : _user = _repository.currentUser {
    final repository = _repository;
    if (repository is AuthStateSource) {
      _busy = true;
      _subscription = (repository as AuthStateSource).authStateChanges.listen(
        (user) {
          if (_disposed || _operation) return;
          _user = user;
          _busy = false;
          _error = null;
          notifyListeners();
        },
        onError: (Object error) {
          if (_disposed || _operation) return;
          _user = null;
          _busy = false;
          _error = error is AuthFailure
              ? error.message
              : 'Unable to restore your session. Please sign in again.';
          notifyListeners();
        },
      );
    }
  }

  StreamSubscription<AuthUser?>? _subscription;
  bool _operation = false;

  final AuthRepository _repository;
  AuthUser? _user;
  bool _busy = false;
  bool _disposed = false;
  String? _error;

  AuthUser? get user => _user;
  bool get isBusy => _busy;
  String? get error => _error;
  bool get supportsRegistration => _repository is CustomerRegistration;

  void clearError() {
    if (_busy || _disposed) return;
    _error = null;
    notifyListeners();
  }

  Future<void> signUp({
    required String fullName,
    required String email,
    required String password,
  }) async {
    if (_busy || _disposed || !supportsRegistration) return;
    _busy = true;
    _operation = true;
    _user = null;
    _error = null;
    notifyListeners();
    try {
      _user = await (_repository as CustomerRegistration).signUp(
        fullName: fullName.trim(), email: email.trim(), password: password,
      );
    } catch (error) {
      _user = null;
      _error = error is AuthFailure
          ? error.message : 'Unable to create your account. Please try again.';
    } finally {
      _operation = false;
      _busy = false;
      if (!_disposed) notifyListeners();
    }
  }
  bool get supportsDevelopmentRoles => _repository is DevelopmentRoleSelection;
  UserRole? get developmentRole => _repository is DevelopmentRoleSelection
      ? (_repository as DevelopmentRoleSelection).selectedRole
      : null;

  void selectDevelopmentRole(UserRole role) {
    if (_busy || !supportsDevelopmentRoles) return;
    (_repository as DevelopmentRoleSelection).selectedRole = role;
    _error = null;
    notifyListeners();
  }

  Future<void> signIn({required String email, required String password}) async {
    if (_busy || _disposed) return;
    _busy = true;
    _operation = true;
    _error = null;
    _user = null;
    notifyListeners();
    try {
      _user = await _repository.signIn(email: email.trim(), password: password);
    } catch (error) {
      _user = null;
      _error = error is AuthFailure
          ? error.message
          : 'Unable to sign in. Please try again.';
    } finally {
      _operation = false;
      _busy = false;
      if (!_disposed) notifyListeners();
    }
  }

  Future<void> signOut() async {
    if (_busy || _disposed) return;
    _busy = true;
    _operation = true;
    _error = null;
    notifyListeners();
    try {
      await _repository.signOut();
      _user = null;
    } catch (_) {
      _error = 'Unable to sign out. Please try again.';
    } finally {
      _operation = false;
      _busy = false;
      if (!_disposed) notifyListeners();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _subscription?.cancel();
    super.dispose();
  }
}
