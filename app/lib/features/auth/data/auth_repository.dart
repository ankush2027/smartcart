import '../models/auth_user.dart';

/// App-level authentication contract; no Firebase types cross this boundary.
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
  const AuthFailure([this.message = 'Unable to sign in. Please try again.']);

  final String message;
}

/// Optional live session capability (including the initial restored session).
abstract interface class AuthStateSource {
  Stream<AuthUser?> get authStateChanges;
}
