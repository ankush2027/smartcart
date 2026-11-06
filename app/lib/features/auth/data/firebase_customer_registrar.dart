import 'dart:async';
import 'dart:convert';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../../../core/config/api_configuration.dart';
import 'auth_repository.dart';

/// Idempotently creates the backend customer identity using verified Firebase
/// claims. No UID, email, or role is accepted from the client payload.
class FirebaseCustomerRegistrar {
  FirebaseCustomerRegistrar({
    String? baseUrl,
    http.Client Function()? clientFactory,
  }) : _baseUrl = baseUrl ?? const String.fromEnvironment('SMARTCART_API_BASE_URL'),
       _clientFactory = clientFactory ?? http.Client.new;

  final String _baseUrl;
  final http.Client Function() _clientFactory;

  Future<void> call(User user) async {
    final Uri endpoint;
    try {
      endpoint = apiEndpoint(
        _baseUrl,
        '/customer/register',
        allowDevelopmentHttp: kDebugMode,
      );
    } on FormatException {
      throw const AuthFailure('Customer service is not configured. Contact the demo operator.');
    }
    try {
      final token = await user.getIdToken(true);
      if (token == null || token.isEmpty) throw const AuthFailure('Please sign in again.');
      final client = _clientFactory();
      try {
        final response = await client
            .post(
              endpoint,
              headers: {
                'Authorization': 'Bearer $token',
                'Content-Type': 'application/json',
              },
              body: jsonEncode(const <String, Object?>{}),
            )
            .timeout(const Duration(seconds: 12));
        final body = jsonDecode(response.body);
        if (response.statusCode != 200 ||
            body is! Map<String, dynamic> ||
            body['status'] != 'registered') {
          throw const AuthFailure('Unable to prepare your customer account. Please try again.');
        }
      } finally {
        client.close();
      }
    } on AuthFailure {
      rethrow;
    } on TimeoutException {
      throw const AuthFailure('Customer registration timed out. Check your connection and retry.');
    } catch (_) {
      throw const AuthFailure('Unable to prepare your customer account. Please try again.');
    }
  }
}
