import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:smartcart/features/auth/data/firebase_customer_registrar.dart';

class FakeFirebaseUser extends Fake implements User {
  @override
  String get uid => 'firebase-issued-uid';
  @override
  Future<String?> getIdToken([bool forceRefresh = false]) async {
    expect(forceRefresh, isTrue);
    return 'fresh-firebase-token';
  }
}

void main() {
  test('registration sends only a fresh bearer token and empty body', () async {
    final client = MockClient((request) async {
      expect(request.method, 'POST');
      expect(request.url.toString(), 'https://demo.trycloudflare.com/customer/register');
      expect(request.headers['authorization'], 'Bearer fresh-firebase-token');
      expect(request.body, '{}');
      return http.Response('{"status":"registered"}', 200);
    });
    await FirebaseCustomerRegistrar(
      baseUrl: 'https://demo.trycloudflare.com',
      clientFactory: () => client,
    ).call(FakeFirebaseUser());
  });

  test('provisioning fails closed when backend refuses registration', () async {
    final client = MockClient((_) async => http.Response('{"error":"invalid_token"}', 401));
    await expectLater(
      FirebaseCustomerRegistrar(
        baseUrl: 'https://demo.trycloudflare.com',
        clientFactory: () => client,
      ).call(FakeFirebaseUser()),
      throwsA(isA<Exception>()),
    );
  });
}
