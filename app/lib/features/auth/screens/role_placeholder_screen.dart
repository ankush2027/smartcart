import 'package:flutter/material.dart';

import '../controllers/auth_controller.dart';
import '../models/auth_user.dart';

class RolePlaceholderScreen extends StatelessWidget {
  const RolePlaceholderScreen({
    super.key,
    required this.controller,
    required this.role,
  });

  final AuthController controller;
  final UserRole role;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('SmartCart')),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  role.label,
                  style: Theme.of(context).textTheme.headlineMedium,
                ),
                const SizedBox(height: 16),
                Text(
                  '${role.label} experience coming in Stage ${role == UserRole.customer ? 3 : 4}',
                  textAlign: TextAlign.center,
                ),
                if (controller.error != null) ...[
                  const SizedBox(height: 16),
                  Semantics(
                    liveRegion: true,
                    child: Text(
                      controller.error!,
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.error,
                      ),
                    ),
                  ),
                ],
                const SizedBox(height: 24),
                FilledButton(
                  onPressed: controller.isBusy ? null : controller.signOut,
                  child: Text(controller.isBusy ? 'Signing out…' : 'Logout'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
