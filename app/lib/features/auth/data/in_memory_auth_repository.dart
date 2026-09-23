import '../models/auth_user.dart';
import 'auth_repository.dart';

/// Demo only: accepts form input, stores no credentials, and resets on restart.
class InMemoryAuthRepository
    implements AuthRepository, DevelopmentRoleSelection {
  InMemoryAuthRepository({this.delay = const Duration(milliseconds: 400)});

  final Duration delay;
  AuthUser? _currentUser;

  @override
  UserRole selectedRole = UserRole.customer;

  @override
  AuthUser? get currentUser => _currentUser;

  @override
  Future<AuthUser> signIn({
    required String email,
    required String password,
  }) async {
    final role = selectedRole;
    await Future<void>.delayed(delay);
    if (!RegExp(r'^[^\s@]+@[^\s@]+\.[^\s@]+$').hasMatch(email.trim()) ||
        password.trim().isEmpty) {
      throw const AuthFailure();
    }
    return _currentUser = AuthUser(role: role);
  }

  @override
  Future<void> signOut() async {
    await Future<void>.delayed(delay);
    _currentUser = null;
    selectedRole = UserRole.customer;
  }
}
