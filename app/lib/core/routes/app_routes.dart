import 'package:flutter/material.dart';

import '../../features/auth/controllers/auth_controller.dart';
import '../../features/auth/screens/login_screen.dart';
import '../../features/auth/screens/role_placeholder_screen.dart';

abstract final class AppRoutes {
  static const authentication = '/';

  // A single guarded root prevents back navigation into a signed-out session.
  static Map<String, WidgetBuilder> routes(AuthController controller) => {
    authentication: (_) => ListenableBuilder(
      listenable: controller,
      builder: (context, child) {
        final user = controller.user;
        return user == null
            ? LoginScreen(controller: controller)
            : RolePlaceholderScreen(controller: controller, role: user.role);
      },
    ),
  };
}
