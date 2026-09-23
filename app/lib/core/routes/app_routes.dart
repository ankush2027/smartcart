import 'package:flutter/material.dart';

import '../../features/launch/launch_screen.dart';

abstract final class AppRoutes {
  static const launch = '/';

  // Add /login, /customer, and /manager here when those features are built.
  static final Map<String, WidgetBuilder> routes = {
    launch: (_) => const LaunchScreen(),
  };
}
