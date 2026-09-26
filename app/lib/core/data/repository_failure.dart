/// Safe app-level failures; never carry raw SDK messages or credentials.
enum RepositoryFailureCode {
  invalidData,
  unauthenticated,
  permissionDenied,
  multipleActiveSessions,
  identityChanged,
  unavailable,
  disposed,
}

class RepositoryFailure implements Exception {
  const RepositoryFailure(this.code);
  final RepositoryFailureCode code;

  String get message => switch (code) {
    RepositoryFailureCode.invalidData => 'Cart data is incomplete or invalid.',
    RepositoryFailureCode.unauthenticated => 'Please sign in again.',
    RepositoryFailureCode.permissionDenied =>
      'You do not have access to this data.',
    RepositoryFailureCode.multipleActiveSessions =>
      'Multiple active sessions were found. Please contact support.',
    RepositoryFailureCode.identityChanged =>
      'Your session changed. Please reload.',
    RepositoryFailureCode.unavailable =>
      'Unable to load data. Please try again.',
    RepositoryFailureCode.disposed => 'This data connection has been closed.',
  };
}
