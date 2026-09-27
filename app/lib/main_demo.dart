import 'package:flutter/material.dart';

import 'app.dart';
import 'features/auth/data/in_memory_auth_repository.dart';
import 'features/customer/data/mock_cart_repository.dart';
import 'features/manager/data/mock_manager_repository.dart';

/// Offline UI preview entrypoint.
/// Runs with the existing in-memory auth and mock repositories
/// so you can view and test all UI screens without requiring live Firebase.
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(
    SmartCartApp(
      authRepository: InMemoryAuthRepository(),
      cartRepository: MockCartRepository(),
      managerRepository: MockManagerRepository(),
    ),
  );
}
