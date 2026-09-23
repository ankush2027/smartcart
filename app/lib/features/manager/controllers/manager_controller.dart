import 'package:flutter/foundation.dart';

import '../data/manager_repository.dart';
import '../models/manager_overview.dart';

enum ManagerLoadState { loading, ready, error }

class ManagerController extends ChangeNotifier {
  ManagerController(this._repository);
  final ManagerRepository _repository;
  ManagerOverview? _overview;
  ManagerLoadState _state = ManagerLoadState.loading;
  bool _inFlight = false;
  bool _disposed = false;
  ManagerOverview? get overview => _overview;
  ManagerLoadState get state => _state;
  bool get isDemo => _repository.isDemo;

  Future<void> load() async {
    if (_disposed || _inFlight) return;
    _inFlight = true;
    _overview = null;
    _state = ManagerLoadState.loading;
    notifyListeners();
    try {
      final overview = await _repository.loadOverview();
      if (_disposed) return;
      _overview = overview;
      _state = ManagerLoadState.ready;
    } catch (_) {
      if (!_disposed) _state = ManagerLoadState.error;
    } finally {
      _inFlight = false;
      if (!_disposed) notifyListeners();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
