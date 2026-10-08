import 'package:flutter/material.dart';

import '../controllers/auth_controller.dart';

class SignupScreen extends StatefulWidget {
  const SignupScreen({super.key, required this.controller, required this.onLogin});
  final AuthController controller;
  final VoidCallback onLogin;

  @override
  State<SignupScreen> createState() => _SignupScreenState();
}

class _SignupScreenState extends State<SignupScreen> {
  final _form = GlobalKey<FormState>();
  final _name = TextEditingController();
  final _email = TextEditingController();
  final _password = TextEditingController();
  final _confirmation = TextEditingController();
  bool _hidden = true;

  @override
  void dispose() {
    _name.dispose();
    _email.dispose();
    _password.dispose();
    _confirmation.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (widget.controller.isBusy || !_form.currentState!.validate()) return;
    FocusScope.of(context).unfocus();
    final password = _password.text;
    _password.clear();
    _confirmation.clear();
    await widget.controller.signUp(
      fullName: _name.text, email: _email.text, password: password,
    );
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.controller,
    builder: (context, _) {
      final auth = widget.controller;
      return Scaffold(
        appBar: AppBar(title: const Text('Create your SmartCart account')),
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
                    children: [
                      Text('Sign Up', style: Theme.of(context).textTheme.headlineMedium),
                      const SizedBox(height: 20),
                      TextFormField(
                        key: const Key('signup_name'), controller: _name,
                        enabled: !auth.isBusy, textCapitalization: TextCapitalization.words,
                        textInputAction: TextInputAction.next,
                        decoration: const InputDecoration(labelText: 'Full name'),
                        validator: (value) => (value?.trim().isEmpty ?? true)
                            ? 'Full name is required.' : null,
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        key: const Key('signup_email'), controller: _email,
                        enabled: !auth.isBusy, keyboardType: TextInputType.emailAddress,
                        autocorrect: false, textInputAction: TextInputAction.next,
                        decoration: const InputDecoration(labelText: 'Email'),
                        validator: (value) => RegExp(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
                            .hasMatch(value?.trim() ?? '') ? null : 'Please enter a valid email address.',
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        key: const Key('signup_password'), controller: _password,
                        enabled: !auth.isBusy, obscureText: _hidden,
                        autocorrect: false, enableSuggestions: false,
                        textInputAction: TextInputAction.next,
                        decoration: InputDecoration(
                          labelText: 'Password',
                          helperText: 'At least 6 characters; additional account rules may apply.',
                          helperMaxLines: 2,
                          suffixIcon: IconButton(
                            tooltip: _hidden ? 'Show passwords' : 'Hide passwords',
                            onPressed: auth.isBusy ? null : () => setState(() => _hidden = !_hidden),
                            icon: Icon(_hidden ? Icons.visibility : Icons.visibility_off),
                          ),
                        ),
                        validator: (value) => value == null || value.length < 6
                            ? 'Use at least 6 characters.' : null,
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        key: const Key('signup_confirmation'), controller: _confirmation,
                        enabled: !auth.isBusy, obscureText: _hidden,
                        autocorrect: false, enableSuggestions: false,
                        textInputAction: TextInputAction.done,
                        onFieldSubmitted: (_) => _submit(),
                        decoration: const InputDecoration(labelText: 'Confirm password'),
                        validator: (value) => (value?.isEmpty ?? true)
                            ? 'Please confirm your password.'
                            : value != _password.text ? 'Passwords do not match.' : null,
                      ),
                      if (auth.error != null) ...[
                        const SizedBox(height: 16),
                        Semantics(liveRegion: true, child: Text(auth.error!,
                          style: TextStyle(color: Theme.of(context).colorScheme.error))),
                      ],
                      const SizedBox(height: 24),
                      FilledButton(
                        key: const Key('submit_signup'), onPressed: auth.isBusy ? null : _submit,
                        child: auth.isBusy
                            ? const SizedBox(width: 20, height: 20,
                                child: CircularProgressIndicator(strokeWidth: 2,
                                  semanticsLabel: 'Creating account'))
                            : const Text('Create Account'),
                      ),
                      TextButton(onPressed: auth.isBusy ? null : widget.onLogin,
                        child: const Text('Already have an account? Log In')),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    },
  );
}
