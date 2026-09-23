import 'package:flutter/foundation.dart';

import '../data/auth_repository.dart';
import '../models/auth_user.dart';

class AuthController extends ChangeNotifier {
  AuthController(this._repository) : _user = _repository.currentUser;

  final AuthRepository _repository;
  AuthUser? _user;
  bool _busy = false;
  bool _disposed = false;
  String? _error;

  AuthUser? get user => _user;
  bool get isBusy => _busy;
  String? get error => _error;
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
    _error = null;
    notifyListeners();
    try {
      _user = await _repository.signIn(email: email.trim(), password: password);
    } catch (_) {
      _error = 'Unable to sign in. Please try again.';
    } finally {
      _busy = false;
      if (!_disposed) notifyListeners();
    }
  }

  Future<void> signOut() async {
    if (_busy || _disposed) return;
    _busy = true;
    _error = null;
    notifyListeners();
    try {
      await _repository.signOut();
      _user = null;
    } catch (_) {
      _error = 'Unable to sign out. Please try again.';
    } finally {
      _busy = false;
      if (!_disposed) notifyListeners();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
