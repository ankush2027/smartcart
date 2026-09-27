import 'dart:async';

import 'package:flutter/foundation.dart';

import '../../../core/data/repository_failure.dart';
import '../data/manager_repository.dart';
import '../models/manager_overview.dart';

enum ManagerLoadState { loading, ready, error }

class ManagerController extends ChangeNotifier {
  ManagerController(
    this._repository, {
    this.discoveryTimeout = const Duration(seconds: 20),
  });
  final ManagerRepository _repository;
  final Duration discoveryTimeout;
  ManagerOverview? _overview;
  ManagerLoadState _state = ManagerLoadState.loading;
  bool _inFlight = false;
  bool _disposed = false;
  int _generation = 0;
  StreamSubscription<ManagerOverview>? _subscription;
  Timer? _timer;
  String? _error;
  String? get error => _error;
  ManagerOverview? get overview => _overview;
  ManagerLoadState get state => _state;
  bool get isDemo => _repository.isDemo;

  void _accept(ManagerOverview overview) {
    _timer?.cancel();
    _overview = overview;
    _error = null;
    _state = ManagerLoadState.ready;
    notifyListeners();
  }

  void _fail(Object error) {
    _timer?.cancel();
    _overview = null;
    _state = ManagerLoadState.error;
    _error = error is RepositoryFailure
        ? error.message
        : 'Unable to load manager data. Please try again.';
    notifyListeners();
  }

  Future<void> load() async {
    if (_disposed || _inFlight) return;
    _inFlight = true;
    final generation = ++_generation;
    _timer?.cancel();
    _overview = null;
    _error = null;
    _state = ManagerLoadState.loading;
    notifyListeners();
    try {
      await _subscription?.cancel();
      _subscription = null;
      if (_disposed || generation != _generation) return;
      final repository = _repository;
      if (repository is RealtimeManagerRepository) {
        _subscription = repository.watchOverview().listen(
          (overview) {
            if (!_disposed && generation == _generation) _accept(overview);
          },
          onError: (Object error) {
            if (!_disposed && generation == _generation) _fail(error);
          },
          onDone: () {
            if (!_disposed &&
                generation == _generation &&
                _state != ManagerLoadState.error) {
              _fail(const RepositoryFailure(RepositoryFailureCode.unavailable));
            }
          },
        );
        _timer = Timer(discoveryTimeout, () {
          if (!_disposed &&
              generation == _generation &&
              _state == ManagerLoadState.loading) {
            _fail(const RepositoryFailure(RepositoryFailureCode.unavailable));
          }
        });
      } else {
        final overview = await repository.loadOverview();
        if (!_disposed && generation == _generation) _accept(overview);
      }
    } catch (error) {
      if (!_disposed && generation == _generation) _fail(error);
    } finally {
      _inFlight = false;
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _generation++;
    _timer?.cancel();
    _subscription?.cancel();
    _overview = null;
    _error = null;
    super.dispose();
  }
}
