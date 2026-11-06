import 'package:flutter/material.dart';

import '../controllers/auth_controller.dart';
import '../models/auth_user.dart';
import 'signup_screen.dart';

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
  bool _signingUp = false;

  void _showSignup(bool value) {
    if (widget.controller.isBusy) return;
    _password.clear();
    widget.controller.clearError();
    setState(() => _signingUp = value);
  }

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
    if (_signingUp) {
      return SignupScreen(controller: widget.controller, onLogin: () => _showSignup(false));
    }
    final auth = widget.controller;
    final theme = Theme.of(context);
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Form(
                key: _form,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(
                      Icons.shopping_cart_outlined,
                      size: 48,
                      color: theme.colorScheme.primary,
                    ),
                    const SizedBox(height: 16),
                    Text(
                      'SmartCart',
                      textAlign: TextAlign.center,
                      style: theme.textTheme.headlineLarge,
                    ),
                    const SizedBox(height: 8),
                    Text(
                      'Welcome Back',
                      textAlign: TextAlign.center,
                      style: theme.textTheme.titleLarge,
                    ),
                    const SizedBox(height: 28),
                    if (auth.supportsDevelopmentRoles) ...[
                      Card.filled(
                        margin: EdgeInsets.zero,
                        child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              Text(
                                'Development demo only',
                                style: theme.textTheme.titleSmall,
                              ),
                              const SizedBox(height: 8),
                              const Text(
                                'Use a made-up email and password. '
                                'No real authentication is connected. '
                                'Choose a temporary test role below.',
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
                      ),
                      const SizedBox(height: 24),
                    ],
                    TextFormField(
                      controller: _email,
                      enabled: !auth.isBusy,
                      keyboardType: TextInputType.emailAddress,
                      textInputAction: TextInputAction.next,
                      autocorrect: false,
                      decoration: const InputDecoration(
                        labelText: 'Email',
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
                                ? Icons.visibility
                                : Icons.visibility_off,
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
                      Semantics(
                        liveRegion: true,
                        child: Text(
                          auth.error!,
                          style: TextStyle(color: theme.colorScheme.error),
                        ),
                      ),
                    ],
                    const SizedBox(height: 24),
                    FilledButton(
                      onPressed: auth.isBusy ? null : _submit,
                      child: Padding(
                        padding: const EdgeInsets.symmetric(vertical: 12),
                        child: auth.isBusy
                            ? const SizedBox(
                                width: 20,
                                height: 20,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  semanticsLabel: 'Signing in',
                                ),
                              )
                            : const Text('Sign In'),
                      ),
                    ),
                    if (auth.supportsGoogleAuthentication) ...[
                      const SizedBox(height: 10),
                      OutlinedButton.icon(
                        key: const Key('google_sign_in'),
                        onPressed: auth.isBusy ? null : auth.signInWithGoogle,
                        icon: const Icon(Icons.account_circle_outlined),
                        label: const Text('Continue with Google'),
                      ),
                    ],
                    if (auth.supportsRegistration) ...[
                      const SizedBox(height: 12),
                      TextButton(
                        key: const Key('open_signup'),
                        onPressed: auth.isBusy ? null : () => _showSignup(true),
                        child: const Text('New to SmartCart? Sign Up'),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
