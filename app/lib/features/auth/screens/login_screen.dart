import 'package:flutter/material.dart';

import '../controllers/auth_controller.dart';
import '../models/auth_user.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key, required this.controller});

  final AuthController controller;

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _form = GlobalKey<FormState>();
  final _email = TextEditingController();
  final _password = TextEditingController();
  bool _obscurePassword = true;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (widget.controller.isBusy || !_form.currentState!.validate()) return;
    FocusScope.of(context).unfocus();
    final password = _password.text;
    _password.clear();
    await widget.controller.signIn(email: _email.text, password: password);
  }

  @override
  Widget build(BuildContext context) {
    final auth = widget.controller;
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 440),
              child: Card(
                elevation: 0,
                color: theme.colorScheme.surface,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(20),
                  side: BorderSide(
                    color: colorScheme.outline.withValues(alpha: 0.8),
                    width: 1.2,
                  ),
                ),
                child: Padding(
                  padding: const EdgeInsets.all(20),
                  child: Form(
                    key: _form,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Center(
                          child: Container(
                            width: 64,
                            height: 64,
                            decoration: BoxDecoration(
                              color: colorScheme.primaryContainer,
                              borderRadius: BorderRadius.circular(20),
                              boxShadow: [
                                BoxShadow(
                                  color: colorScheme.primary.withValues(alpha: 0.12),
                                  blurRadius: 16,
                                  offset: const Offset(0, 4),
                                ),
                              ],
                            ),
                            child: Icon(
                              Icons.shopping_cart_outlined,
                              size: 32,
                              color: colorScheme.primary,
                            ),
                          ),
                        ),
                        const SizedBox(height: 20),
                        Text(
                          'SmartCart',
                          textAlign: TextAlign.center,
                          style: theme.textTheme.headlineLarge?.copyWith(
                            fontWeight: FontWeight.w800,
                            letterSpacing: -0.5,
                            color: colorScheme.onSurface,
                          ),
                        ),
                        const SizedBox(height: 6),
                        Text(
                          'Welcome Back',
                          textAlign: TextAlign.center,
                          style: theme.textTheme.titleMedium?.copyWith(
                            color: colorScheme.onSurfaceVariant,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                        const SizedBox(height: 28),
                        if (auth.supportsDevelopmentRoles) ...[
                          Container(
                            decoration: BoxDecoration(
                              color: colorScheme.primaryContainer.withValues(alpha: 0.4),
                              borderRadius: BorderRadius.circular(14),
                              border: Border.all(
                                color: colorScheme.primary.withValues(alpha: 0.25),
                              ),
                            ),
                            padding: const EdgeInsets.all(16),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                Row(
                                  children: [
                                    Icon(
                                      Icons.developer_mode_rounded,
                                      size: 18,
                                      color: colorScheme.primary,
                                    ),
                                    const SizedBox(width: 8),
                                    Expanded(
                                      child: Text(
                                        'Development demo only',
                                        style: theme.textTheme.titleSmall?.copyWith(
                                          color: colorScheme.primary,
                                          fontWeight: FontWeight.w700,
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 8),
                                Text(
                                  'Use a made-up email and password. '
                                  'No real authentication is connected. '
                                  'Choose a temporary test role below.',
                                  style: theme.textTheme.bodySmall?.copyWith(
                                    color: colorScheme.onSurfaceVariant,
                                    height: 1.4,
                                  ),
                                ),
                                const SizedBox(height: 12),
                                DropdownButtonFormField<UserRole>(
                                  isExpanded: true,
                                  initialValue: auth.developmentRole,
                                  decoration: const InputDecoration(
                                    labelText: 'Temporary test role',
                                    border: OutlineInputBorder(),
                                  ),
                                  items: UserRole.values
                                      .map(
                                        (role) => DropdownMenuItem(
                                          value: role,
                                          child: Text(role.label),
                                        ),
                                      )
                                      .toList(),
                                  onChanged: auth.isBusy
                                      ? null
                                      : (role) {
                                          if (role != null) {
                                            auth.selectDevelopmentRole(role);
                                          }
                                        },
                                ),
                              ],
                            ),
                          ),
                          const SizedBox(height: 20),
                        ],
                        TextFormField(
                          controller: _email,
                          enabled: !auth.isBusy,
                          keyboardType: TextInputType.emailAddress,
                          textInputAction: TextInputAction.next,
                          autocorrect: false,
                          decoration: const InputDecoration(
                            labelText: 'Email',
                            prefixIcon: Icon(Icons.mail_outline_rounded, size: 20),
                            border: OutlineInputBorder(),
                          ),
                          validator: (value) =>
                              RegExp(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
                                      .hasMatch(value?.trim() ?? '')
                                  ? null
                                  : 'Please enter a valid email address.',
                        ),
                        const SizedBox(height: 16),
                        TextFormField(
                          controller: _password,
                          enabled: !auth.isBusy,
                          obscureText: _obscurePassword,
                          autocorrect: false,
                          enableSuggestions: false,
                          textInputAction: TextInputAction.done,
                          onFieldSubmitted: (_) => _submit(),
                          decoration: InputDecoration(
                            labelText: 'Password',
                            prefixIcon: const Icon(Icons.lock_outline_rounded, size: 20),
                            border: const OutlineInputBorder(),
                            suffixIcon: IconButton(
                              tooltip: _obscurePassword
                                  ? 'Show password'
                                  : 'Hide password',
                              onPressed: auth.isBusy
                                  ? null
                                  : () => setState(() {
                                      _obscurePassword = !_obscurePassword;
                                    }),
                              icon: Icon(
                                _obscurePassword
                                    ? Icons.visibility_outlined
                                    : Icons.visibility_off_outlined,
                                size: 20,
                              ),
                            ),
                          ),
                          validator: (value) =>
                              value == null || value.trim().isEmpty
                                  ? 'Password is required.'
                                  : null,
                        ),
                        if (auth.error != null) ...[
                          const SizedBox(height: 16),
                          Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 12,
                              vertical: 10,
                            ),
                            decoration: BoxDecoration(
                              color: colorScheme.error.withValues(alpha: 0.08),
                              borderRadius: BorderRadius.circular(10),
                              border: Border.all(
                                color: colorScheme.error.withValues(alpha: 0.2),
                              ),
                            ),
                            child: Row(
                              children: [
                                Icon(
                                  Icons.error_outline_rounded,
                                  size: 18,
                                  color: colorScheme.error,
                                ),
                                const SizedBox(width: 8),
                                Expanded(
                                  child: Semantics(
                                    liveRegion: true,
                                    child: Text(
                                      auth.error!,
                                      style: TextStyle(
                                        color: colorScheme.error,
                                        fontSize: 13,
                                        fontWeight: FontWeight.w500,
                                      ),
                                    ),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ],
                        const SizedBox(height: 24),
                        FilledButton(
                          onPressed: auth.isBusy ? null : _submit,
                          style: FilledButton.styleFrom(
                            padding: const EdgeInsets.symmetric(vertical: 15),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(12),
                            ),
                          ),
                          child: auth.isBusy
                              ? const SizedBox(
                                  width: 20,
                                  height: 20,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                    valueColor: AlwaysStoppedAnimation<Color>(
                                      Colors.white,
                                    ),
                                    semanticsLabel: 'Signing in',
                                  ),
                                )
                              : const Text(
                                  'Sign In',
                                  style: TextStyle(
                                    fontSize: 16,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                        ),
                      ],
                    ),
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
