enum UserRole {
  customer('Customer'),
  manager('Manager');

  const UserRole(this.label);
  final String label;
}

class AuthUser {
  const AuthUser({required this.role});

  final UserRole role;
}
