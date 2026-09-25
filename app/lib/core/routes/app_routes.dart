import 'package:flutter/material.dart';

import '../../features/auth/controllers/auth_controller.dart';
import '../../features/auth/screens/login_screen.dart';
import '../../features/auth/models/auth_user.dart';
import '../../features/customer/data/cart_repository.dart';
import '../../features/customer/screens/customer_screen.dart';
import '../../features/manager/data/manager_repository.dart';
import '../../features/manager/screens/manager_screen.dart';

abstract final class AppRoutes {
  static const authentication = '/';

  // A single guarded root prevents back navigation into a signed-out session.
  static Map<String, WidgetBuilder> routes(
    AuthController controller,
    CartRepository cartRepository,
    ManagerRepository managerRepository,
  ) => {
    authentication: (_) => ListenableBuilder(
      listenable: controller,
      builder: (context, child) {
        final user = controller.user;
        return user == null
            ? LoginScreen(controller: controller)
            : user.role == UserRole.customer
            ? CustomerScreen(
                key: ValueKey(user.uid),
                auth: controller,
                repository: cartRepository,
              )
            : ManagerScreen(
                key: ValueKey(user.uid),
                auth: controller,
                repository: managerRepository,
              );
      },
    ),
  };
}
