import 'dart:async';
import 'dart:convert';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:smartcart/features/customer/services/cart_pairing_service.dart';

class TestTokens implements PairingTokenProvider {
  @override
  String? currentUid = 'uid-a';
  int calls = 0;
  Object? error;
  @override
  Future<PairingToken> freshToken() async {
    calls++;
    if (error != null) throw error!;
    return PairingToken(currentUid!, 'token-$currentUid');
  }
}

class TokenResult extends Fake implements IdTokenResult {
  TokenResult({this.claims, this.token = 'fresh-test-token'});
  @override
  final Map<String, dynamic>? claims;
  @override
  final String? token;
}

class TokenUser extends Fake implements User {
  @override
  String get uid => 'firebase-uid';
  bool? forceRefresh;
  Object? error;
  IdTokenResult result = TokenResult();
  Completer<IdTokenResult>? pending;
  @override
  Future<IdTokenResult> getIdTokenResult([bool forceRefresh = false]) async {
    this.forceRefresh = forceRefresh;
    if (error != null) throw error!;
    return pending == null ? result : pending!.future;
  }
}

class TokenAuth extends Fake implements FirebaseAuth {
  @override
  User? currentUser;
}

Matcher pairingFailure(String code) =>
    isA<PairingFailure>().having((e) => e.code, 'code', code);
void main() {
  const config = PairingConfiguration(
    baseUrl: 'https://edge.example.test/api/',
  );
  test('base URL normalizes and preserves prefix', () {
    expect(
      config.endpoint.toString(),
      'https://edge.example.test/api/pair-cart',
    );
    expect(
      const PairingConfiguration(baseUrl: ' https://edge.example.test/// ')
          .endpoint
          .path,
      '/pair-cart',
    );
  });
  for (final url in [
    '',
    'not-a-url',
    '/relative',
    'ftp://edge.test',
    'https://user:pass@edge.test',
    'https://edge.test?key=x',
    'https://edge.test#x',
    'https://edge.test:0',
    'https://edge.test:wrong',
    'https://edge.test:65536',
  ]) {
    test('invalid config is controlled: $url', () {
      expect(
        () => PairingConfiguration(baseUrl: url).endpoint,
        throwsA(pairingFailure('configuration')),
      );
    });
  }
  test('HTTP allowed only by explicit development configuration', () {
    expect(
      () => const PairingConfiguration(
        baseUrl: 'http://edge.test',
        allowDevelopmentHttp: false,
      ).endpoint,
      throwsA(pairingFailure('configuration')),
    );
    expect(
      const PairingConfiguration(
        baseUrl: 'http://edge.test:5000',
        allowDevelopmentHttp: true,
      ).endpoint.port,
      5000,
    );
  });
  for (final status in ['paired', 'already_paired']) {
    test('$status sends only cart ID and Bearer token', () async {
      final tokens = TestTokens();
      final service = HttpCartPairingService(
        tokens: tokens,
        configuration: config,
        clientFactory: () => MockClient((request) async {
          expect(request.method, 'POST');
          expect(request.url, config.endpoint);
          expect(request.headers['Authorization'], 'Bearer token-uid-a');
          expect(request.headers['Content-Type'], contains('application/json'));
          expect(jsonDecode(request.body), {'cart_id': 'CART-001'});
          expect(request.url.query, isEmpty);
          expect(request.followRedirects, isFalse);
          return http.Response(
            jsonEncode({
              'session_id': 42,
              'cart_id': 'CART-001',
              'status': status,
            }),
            200,
          );
        }),
      );
      await service.pair(' CART-001 ');
      expect(tokens.calls, 1);
    });
  }
  for (final code in [
    'invalid_cart_id',
    'authentication_required',
    'invalid_token',
    'no_active_session_for_cart',
    'cart_already_assigned',
    'customer_already_has_active_session',
    'multiple_active_sessions_for_cart',
    'multiple_active_sessions_for_customer',
    'internal_error',
  ]) {
    test('safe backend error: $code', () async {
      final service = HttpCartPairingService(
        tokens: TestTokens(),
        configuration: config,
        clientFactory: () => MockClient(
          (_) async => http.Response(
            jsonEncode({'error': code, 'message': 'private stack trace'}),
            409,
          ),
        ),
      );
      await expectLater(
        service.pair('CART-001'),
        throwsA(pairingFailure(code)),
      );
      expect(PairingFailure(code).message, isNot(contains('private')));
    });
  }
  for (final status in [401, 403]) {
    test('backend rejects token $status even without JSON', () async {
      final service = HttpCartPairingService(
        tokens: TestTokens(),
        configuration: config,
        clientFactory: () =>
            MockClient((_) async => http.Response('private', status)),
      );
      await expectLater(
        service.pair('CART-001'),
        throwsA(pairingFailure('invalid_token')),
      );
    });
  }
  for (final body in [
    'invalid-json',
    '[]',
    '{}',
    '{"status":"paired","session_id":42,"cart_id":"wrong"}',
  ]) {
    test('malformed success is rejected: $body', () async {
      final service = HttpCartPairingService(
        tokens: TestTokens(),
        configuration: config,
        clientFactory: () => MockClient((_) async => http.Response(body, 200)),
      );
      await expectLater(
        service.pair('CART-001'),
        throwsA(pairingFailure('malformed_response')),
      );
    });
  }
  test('unexpected status does not become success', () async {
    final service = HttpCartPairingService(
      tokens: TestTokens(),
      configuration: config,
      clientFactory: () => MockClient((_) async => http.Response('{}', 302)),
    );
    await expectLater(
      service.pair('CART-001'),
      throwsA(pairingFailure('internal_error')),
    );
  });
  test('unreachable API and timeout are controlled', () async {
    final service = HttpCartPairingService(
      tokens: TestTokens(),
      configuration: config,
      clientFactory: () =>
          MockClient((_) async => throw http.ClientException('private')),
    );
    await expectLater(
      service.pair('CART-001'),
      throwsA(pairingFailure('network')),
    );
    final pending = Completer<http.Response>();
    final timed = HttpCartPairingService(
      tokens: TestTokens(),
      configuration: config,
      timeout: const Duration(milliseconds: 1),
      clientFactory: () => MockClient((_) => pending.future),
    );
    await expectLater(
      timed.pair('CART-001'),
      throwsA(pairingFailure('timeout')),
    );
    pending.complete(http.Response('{}', 500));
  });
  test(
    'new attempt obtains new user token and late old-user response is rejected',
    () async {
      final tokens = TestTokens();
      final headers = <String?>[];
      final pending = Completer<http.Response>();
      final service = HttpCartPairingService(
        tokens: tokens,
        configuration: config,
        clientFactory: () => MockClient((r) {
          headers.add(r.headers['Authorization']);
          return pending.future;
        }),
      );
      final old = expectLater(
        service.pair('CART-001'),
        throwsA(pairingFailure('invalid_token')),
      );
      await Future<void>.delayed(Duration.zero);
      tokens.currentUid = 'uid-b';
      pending.complete(
        http.Response(
          '{"status":"paired","session_id":42,"cart_id":"CART-001"}',
          200,
        ),
      );
      await old;
      await service.pair('CART-001');
      expect(headers, ['Bearer token-uid-a', 'Bearer token-uid-b']);
    },
  );
  test('token failure does not send any HTTP request', () async {
    final tokens = TestTokens()..error = const PairingFailure('invalid_token');
    final service = HttpCartPairingService(
      tokens: tokens,
      configuration: config,
      clientFactory: () => throw StateError('must not create HTTP client'),
    );
    await expectLater(
      service.pair('CART-001'),
      throwsA(pairingFailure('invalid_token')),
    );
  });
  test(
    'Firebase provider force-refreshes token, checks claim and rejects logout',
    () async {
      final user = TokenUser();
      final auth = TokenAuth()..currentUser = user;
      final provider = FirebasePairingTokenProvider(auth: auth);
      expect((await provider.freshToken()).value, 'fresh-test-token');
      expect(user.forceRefresh, isTrue);
      user.result = TokenResult(claims: {'role': 'manager'});
      await expectLater(
        provider.freshToken(),
        throwsA(pairingFailure('invalid_token')),
      );
      user.pending = Completer<IdTokenResult>();
      final result = expectLater(
        provider.freshToken(),
        throwsA(pairingFailure('invalid_token')),
      );
      auth.currentUser = null;
      user.pending!.complete(TokenResult());
      await result;
      await expectLater(
        provider.freshToken(),
        throwsA(pairingFailure('authentication_required')),
      );
    },
  );
  test('Firebase network token failure is controlled', () async {
    final user = TokenUser()
      ..error = FirebaseAuthException(code: 'network-request-failed');
    final provider = FirebasePairingTokenProvider(
      auth: TokenAuth()..currentUser = user,
    );
    await expectLater(
      provider.freshToken(),
      throwsA(pairingFailure('network')),
    );
  });
}
