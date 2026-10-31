import 'package:flutter/foundation.dart';

/// One build-time endpoint shared by pairing and checkout. Release requires TLS.
Uri apiEndpoint(String baseUrl, String path, {bool allowDevelopmentHttp = kDebugMode}) {
  final uri = Uri.tryParse(baseUrl.trim());
  if (uri == null || !uri.hasAuthority || uri.host.isEmpty ||
      uri.userInfo.isNotEmpty || uri.hasQuery || uri.hasFragment ||
      uri.port < 1 || uri.port > 65535 ||
      (uri.scheme != 'https' && !(uri.scheme == 'http' && allowDevelopmentHttp && !kReleaseMode))) {
    throw const FormatException('A valid HTTPS API base URL is required.');
  }
  return uri.replace(path: '${uri.path.replaceFirst(RegExp(r'/+$'), '')}$path');
}
