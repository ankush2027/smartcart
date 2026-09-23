import '../models/auth_user.dart';

/// Replace this implementation with Firebase during the integration stage.
abstract interface class AuthRepository {
  AuthUser? get currentUser;
  Future<AuthUser> signIn({required String email, required String password});
  Future<void> signOut();
}

/// Optional development capability; real authentication resolves its own roles.
abstract interface class DevelopmentRoleSelection {
  UserRole get selectedRole;
  set selectedRole(UserRole role);
}

class AuthFailure implements Exception {
  const AuthFailure();
}
