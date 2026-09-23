import 'package:flutter/material.dart';

import 'core/routes/app_routes.dart';
import 'core/theme/app_theme.dart';
import 'features/auth/controllers/auth_controller.dart';
import 'features/auth/data/auth_repository.dart';
import 'features/auth/data/in_memory_auth_repository.dart';

class SmartCartApp extends StatefulWidget {
  const SmartCartApp({super.key, this.authRepository});

  final AuthRepository? authRepository;

  @override
  State<SmartCartApp> createState() => _SmartCartAppState();
}

class _SmartCartAppState extends State<SmartCartApp> {
  late final AuthController _auth = AuthController(
    widget.authRepository ?? InMemoryAuthRepository(),
  );

  @override
  void dispose() {
    _auth.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'SmartCart',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light,
      initialRoute: AppRoutes.authentication,
      routes: AppRoutes.routes(_auth),
    );
  }
}
