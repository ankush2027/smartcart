import 'dart:async';
import 'dart:convert';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

abstract interface class CartPairingService {
  Future<void> pair(String cartId);
}

class PairingFailure implements Exception {
  const PairingFailure(this.code);
  final String code;
  String get message => switch (code) {
    'configuration' =>
      'Cart connection is not configured. Please contact support.',
    'invalid_cart_id' => 'Enter a valid Cart ID.',
    'authentication_required' ||
    'invalid_token' => 'Please sign in again before connecting a cart.',
    'no_active_session_for_cart' =>
      'No active shopping session was found for this Cart ID.',
    'cart_already_assigned' =>
      'This cart is already connected to another customer.',
    'customer_already_has_active_session' =>
      'Your account is already connected to another active cart.',
    'multiple_active_sessions_for_cart' ||
    'multiple_active_sessions_for_customer' =>
      'Multiple active sessions were found. Please contact support.',
    'timeout' =>
      'Cart connection timed out. Check your connection and try again.',
    'network' =>
      'Cannot reach the cart service. Check your connection and try again.',
    _ => 'Unable to connect the cart. Please try again.',
  };
}

class PairingConfiguration {
  const PairingConfiguration({
    this.baseUrl = const String.fromEnvironment('SMARTCART_API_BASE_URL'),
    this.allowDevelopmentHttp = kDebugMode,
  });
  final String baseUrl;
  final bool allowDevelopmentHttp;
  Uri get endpoint {
    final uri = Uri.tryParse(baseUrl.trim());
    if (uri == null ||
        !uri.hasAuthority ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.scheme != 'https' &&
            !(uri.scheme == 'http' && allowDevelopmentHttp)) ||
        uri.port < 1 ||
        uri.port > 65535) {
      throw const PairingFailure('configuration');
    }
    final path = uri.path.replaceFirst(RegExp(r'/+$'), '');
    return uri.replace(path: '$path/pair-cart');
  }
}

class PairingToken {
  const PairingToken(this.uid, this.value);
  final String uid;
  final String value;
}

abstract interface class PairingTokenProvider {
  String? get currentUid;
  Future<PairingToken> freshToken();
}

/// Firebase stays behind this authentication boundary; no tokens are stored.
class FirebasePairingTokenProvider implements PairingTokenProvider {
  FirebasePairingTokenProvider({FirebaseAuth? auth}) : _injected = auth;
  final FirebaseAuth? _injected;
  FirebaseAuth get _auth => _injected ?? FirebaseAuth.instance;
  @override
  String? get currentUid => _auth.currentUser?.uid;
  @override
  Future<PairingToken> freshToken() async {
    final user = _auth.currentUser;
    if (user == null) throw const PairingFailure('authentication_required');
    try {
      final result = await user.getIdTokenResult(true);
      if (currentUid != user.uid ||
          result.claims?['role'] == 'manager' ||
          result.token == null ||
          result.token!.isEmpty) {
        throw const PairingFailure('invalid_token');
      }
      return PairingToken(user.uid, result.token!);
    } on FirebaseAuthException catch (error) {
      throw PairingFailure(
        error.code == 'network-request-failed' ? 'network' : 'invalid_token',
      );
    }
  }
}

class HttpCartPairingService implements CartPairingService {
  HttpCartPairingService({
    PairingTokenProvider? tokens,
    this.configuration = const PairingConfiguration(),
    http.Client Function()? clientFactory,
    this.timeout = const Duration(seconds: 15),
  }) : _tokens = tokens ?? FirebasePairingTokenProvider(),
       _clientFactory = clientFactory ?? http.Client.new;
  final PairingTokenProvider _tokens;
  final http.Client Function() _clientFactory;
  final PairingConfiguration configuration;
  final Duration timeout;

  @override
  Future<void> pair(String cartId) async {
    final id = cartId.trim();
    if (id.isEmpty) throw const PairingFailure('invalid_cart_id');
    final endpoint = configuration.endpoint;
    PairingToken token;
    try {
      token = await _tokens.freshToken().timeout(timeout);
    } on PairingFailure {
      rethrow;
    } on TimeoutException {
      throw const PairingFailure('timeout');
    } catch (_) {
      throw const PairingFailure('invalid_token');
    }
    if (_tokens.currentUid != token.uid) {
      throw const PairingFailure('invalid_token');
    }
    final client = _clientFactory();
    try {
      final request = http.Request('POST', endpoint)
        ..followRedirects = false
        ..headers.addAll({
          'Authorization': 'Bearer ${token.value}',
          'Content-Type': 'application/json',
        })
        ..body = jsonEncode({'cart_id': id});
      final response = await client
          .send(request)
          .then(http.Response.fromStream)
          .timeout(timeout);
      if (_tokens.currentUid != token.uid) {
        throw const PairingFailure('invalid_token');
      }
      if (response.statusCode == 401 || response.statusCode == 403) {
        throw const PairingFailure('invalid_token');
      }
      final Object? decoded;
      try {
        decoded = jsonDecode(response.body);
      } catch (_) {
        throw const PairingFailure('malformed_response');
      }
      if (decoded is! Map<String, dynamic>) {
        throw const PairingFailure('malformed_response');
      }
      if (response.statusCode == 200) {
        if (!['paired', 'already_paired'].contains(decoded['status']) ||
            decoded['cart_id'] != id ||
            decoded['session_id'] is! int ||
            (decoded['session_id'] as int) <= 0) {
          throw const PairingFailure('malformed_response');
        }
        // HTTP acknowledges assignment only. Live cart data comes from RTDB.
        return;
      }
      const known = {
        'invalid_cart_id',
        'authentication_required',
        'invalid_token',
        'no_active_session_for_cart',
        'cart_already_assigned',
        'customer_already_has_active_session',
        'multiple_active_sessions_for_cart',
        'multiple_active_sessions_for_customer',
        'internal_error',
      };
      final code = decoded['error'];
      throw PairingFailure(
        known.contains(code) ? code as String : 'internal_error',
      );
    } on PairingFailure {
      rethrow;
    } on TimeoutException {
      throw const PairingFailure('timeout');
    } catch (_) {
      throw const PairingFailure('network');
    } finally {
      client.close();
    }
  }
}
