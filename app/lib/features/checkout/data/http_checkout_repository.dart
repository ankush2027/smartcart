/// HTTP implementation of [CheckoutRepository].
///
/// - Base URL is centralised; never scattered through widget code.
/// - Each request fetches a fresh Firebase ID token — never stored.
/// - Tokens are never logged completely.
/// - All JSON is parsed defensively; backend is the sole authority.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:flutter/foundation.dart';
import '../../../core/config/api_configuration.dart';

import '../models/checkout_models.dart';
import 'checkout_repository.dart';

/// Centralised backend configuration. Reads SMARTCART_API_BASE_URL compile-time
/// constant or accepts an explicit override (useful in tests).
class BackendConfiguration {
  const BackendConfiguration({
    String? baseUrl,
    this.allowDevelopmentHttp = kDebugMode,
  }) : _explicitBaseUrl = baseUrl;

  final String? _explicitBaseUrl;
  final bool allowDevelopmentHttp;

  String get baseUrl {
    final env = const String.fromEnvironment('SMARTCART_API_BASE_URL');
    final url = (_explicitBaseUrl ?? env).trim().replaceAll(RegExp(r'/+$'), '');
    return url;
  }
  Uri endpoint(String path) {
    try {
      return apiEndpoint(baseUrl, path, allowDevelopmentHttp: allowDevelopmentHttp);
    } on FormatException {
      throw const CheckoutFailure('configuration', 'Cart connection is not configured.');
    }
  }

}

class HttpCheckoutRepository implements CheckoutRepository {
  HttpCheckoutRepository({
    required this.tokens,
    BackendConfiguration? configuration,
    http.Client Function()? clientFactory,
    this.timeout = const Duration(seconds: 15),
  })  : configuration = configuration ?? const BackendConfiguration(),
        _clientFactory = clientFactory ?? http.Client.new;

  final CheckoutTokenProvider tokens;
  final BackendConfiguration configuration;
  final Duration timeout;
  final http.Client Function() _clientFactory;

  // -------------------------------------------------------------------------
  // Checkout endpoints
  // -------------------------------------------------------------------------

  @override
  Future<CheckoutResult> beginCheckout({
    required int sessionId,
    required String idempotencyKey,
  }) async {
    final json = await _post(
      '/sessions/$sessionId/checkout',
      {},
      extraHeaders: {'Idempotency-Key': idempotencyKey},
    );
    return CheckoutResult.fromJson(json);
  }

  @override
  Future<CheckoutResult> cancelCheckout({
    required int sessionId,
    required int checkoutId,
  }) async {
    final json = await _post(
      '/sessions/$sessionId/checkouts/$checkoutId/cancel',
      {},
    );
    return CheckoutResult.fromJson(json);
  }

  @override
  Future<Map<String, dynamic>> readCheckoutState({
    required int sessionId,
  }) async {
    return _get('/sessions/$sessionId/checkout');
  }

  // -------------------------------------------------------------------------
  // Payment endpoints
  // -------------------------------------------------------------------------

  @override
  Future<PaymentAttempt> startPayment({
    required int sessionId,
    required int checkoutId,
    required String idempotencyKey,
  }) async {
    final json = await _post(
      '/sessions/$sessionId/checkouts/$checkoutId/payment',
      {},
      extraHeaders: {'Idempotency-Key': idempotencyKey},
    );
    return PaymentAttempt.fromJson(json);
  }

  @override
  Future<PaymentAttempt> paymentStatus({
    required int sessionId,
    required int checkoutId,
  }) async {
    final json = await _get(
      '/sessions/$sessionId/checkouts/$checkoutId/payment',
    );
    return PaymentAttempt.fromJson(json);
  }

  // -------------------------------------------------------------------------
  // Demo endpoint — DEMO ONLY
  // -------------------------------------------------------------------------

  @override
  Future<PaymentAttempt> simulateDemoPaymentSuccess({
    required int sessionId,
    required int checkoutId,
  }) async {
    final json = await _post(
      '/demo/sessions/$sessionId/checkouts/$checkoutId/simulate-payment-success',
      {},
    );
    return PaymentAttempt.fromJson(json);
  }

  // -------------------------------------------------------------------------
  // Internal HTTP helpers
  // -------------------------------------------------------------------------

  Future<String> _freshToken() async {
    try {
      return await tokens.freshIdToken().timeout(timeout);
    } on CheckoutFailure {
      rethrow;
    } on TimeoutException {
      throw const CheckoutFailure('timeout');
    } catch (_) {
      throw const CheckoutFailure('authentication_required');
    }
  }

  Future<Map<String, dynamic>> _post(
    String path,
    Object body, {
    Map<String, String> extraHeaders = const {},
  }) async {
    final token = await _freshToken();
    final client = _clientFactory();
    try {
      final uri = configuration.endpoint(path);
      final request = http.Request('POST', uri)
        ..followRedirects = false
        ..headers.addAll({
          'Authorization': 'Bearer $token',
          'Content-Type': 'application/json',
          ...extraHeaders,
        })
        ..body = jsonEncode(body);
      final response = await client
          .send(request)
          .then(http.Response.fromStream)
          .timeout(timeout);
      return _handleResponse(response);
    } on CheckoutFailure {
      rethrow;
    } on TimeoutException {
      throw const CheckoutFailure('timeout');
    } catch (_) {
      throw const CheckoutFailure('network');
    } finally {
      client.close();
    }
  }

  Future<Map<String, dynamic>> _get(String path) async {
    final token = await _freshToken();
    final client = _clientFactory();
    try {
      final uri = configuration.endpoint(path);
      final request = http.Request('GET', uri)
        ..followRedirects = false
        ..headers['Authorization'] = 'Bearer $token';
      final response = await client
          .send(request)
          .then(http.Response.fromStream)
          .timeout(timeout);
      return _handleResponse(response);
    } on CheckoutFailure {
      rethrow;
    } on TimeoutException {
      throw const CheckoutFailure('timeout');
    } catch (_) {
      throw const CheckoutFailure('network');
    } finally {
      client.close();
    }
  }

  Map<String, dynamic> _handleResponse(http.Response response) {
    final Map<String, dynamic> body;
    try {
      final decoded = jsonDecode(response.body);
      if (decoded is! Map<String, dynamic>) throw const FormatException();
      body = decoded;
    } catch (_) {
      throw const CheckoutFailure('network', 'Malformed response from server.');
    }

    if (response.statusCode >= 200 && response.statusCode < 300) {
      return body;
    }

    if (response.statusCode == 403 && body['error'] == 'preview_access_denied') {
      throw const CheckoutFailure('preview_access_denied');
    }
    if (response.statusCode == 401 || response.statusCode == 403) {
      throw const CheckoutFailure('authentication_required');
    }

    final code = body['error'];
    throw CheckoutFailure(
      code is String && code.isNotEmpty ? code : 'internal_error',
      body['message'] as String?,
    );
  }
}
