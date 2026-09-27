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
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            Container(
              padding: const EdgeInsets.all(6),
              decoration: BoxDecoration(
                color: colorScheme.primaryContainer,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Icon(
                Icons.shopping_cart_outlined,
                size: 18,
                color: colorScheme.primary,
              ),
            ),
            const SizedBox(width: 10),
            const Text('SmartCart'),
          ],
        ),
      ),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 440),
              child: Card(
                elevation: 0,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(20),
                  side: BorderSide(
                    color: colorScheme.outline.withValues(alpha: 0.8),
                    width: 1.2,
                  ),
                ),
                child: Padding(
                  padding: const EdgeInsets.all(32),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Container(
                        padding: const EdgeInsets.all(16),
                        decoration: BoxDecoration(
                          color: colorScheme.primaryContainer.withValues(alpha: 0.5),
                          shape: BoxShape.circle,
                        ),
                        child: Icon(
                          role == UserRole.customer
                              ? Icons.person_outline_rounded
                              : Icons.admin_panel_settings_outlined,
                          size: 36,
                          color: colorScheme.primary,
                        ),
                      ),
                      const SizedBox(height: 20),
                      Text(
                        role.label,
                        style: theme.textTheme.headlineMedium?.copyWith(
                          fontWeight: FontWeight.w800,
                          letterSpacing: -0.4,
                        ),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        '${role.label} experience coming in Stage ${role == UserRole.customer ? 3 : 4}',
                        textAlign: TextAlign.center,
                        style: theme.textTheme.bodyMedium?.copyWith(
                          color: colorScheme.onSurfaceVariant,
                          height: 1.4,
                        ),
                      ),
                      if (controller.error != null) ...[
                        const SizedBox(height: 16),
                        Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 14,
                            vertical: 10,
                          ),
                          decoration: BoxDecoration(
                            color: colorScheme.error.withValues(alpha: 0.08),
                            borderRadius: BorderRadius.circular(10),
                            border: BorderSide(
                              color: colorScheme.error.withValues(alpha: 0.2),
                            ),
                          ),
                          child: Semantics(
                            liveRegion: true,
                            child: Text(
                              controller.error!,
                              textAlign: TextAlign.center,
                              style: TextStyle(
                                color: colorScheme.error,
                                fontWeight: FontWeight.w500,
                              ),
                            ),
                          ),
                        ),
                      ],
                      const SizedBox(height: 24),
                      FilledButton(
                        onPressed: controller.isBusy ? null : controller.signOut,
                        style: FilledButton.styleFrom(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 28,
                            vertical: 14,
                          ),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(12),
                          ),
                        ),
                        child: Text(controller.isBusy ? 'Signing out…' : 'Logout'),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
