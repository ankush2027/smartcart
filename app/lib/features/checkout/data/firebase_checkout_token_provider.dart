/// Firebase-backed [CheckoutTokenProvider].
///
/// Fetches a fresh Firebase ID token for every authenticated HTTP request.
/// Tokens are never stored persistently; no token content is logged.
library;

import 'package:firebase_auth/firebase_auth.dart';

import 'checkout_repository.dart';

class FirebaseCheckoutTokenProvider implements CheckoutTokenProvider {
  FirebaseCheckoutTokenProvider({FirebaseAuth? auth})
    : _auth = auth ?? FirebaseAuth.instance;

  final FirebaseAuth _auth;

  @override
  String? get currentUid => _auth.currentUser?.uid;

  @override
  Future<String> freshIdToken() async {
    final user = _auth.currentUser;
    if (user == null) throw const CheckoutFailure('authentication_required');
    try {
      final result = await user.getIdTokenResult(true);
      final token = result.token;
      if (token == null || token.isEmpty) {
        throw const CheckoutFailure('authentication_required');
      }
      return token;
    } on CheckoutFailure {
      rethrow;
    } on FirebaseAuthException catch (e) {
      throw CheckoutFailure(
        e.code == 'network-request-failed'
            ? 'network'
            : 'authentication_required',
      );
    } catch (_) {
      throw const CheckoutFailure('authentication_required');
    }
  }
}
