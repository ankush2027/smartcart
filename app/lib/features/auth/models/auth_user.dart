enum UserRole {
  customer('Customer'),
  manager('Manager');

  const UserRole(this.label);
  final String label;
}

class AuthUser {
  const AuthUser({required this.uid, required this.role});

  /// Firebase UID in production; never a cart or session identifier.
  final String uid;
  final UserRole role;
}
