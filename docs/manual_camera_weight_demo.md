# Camera + manual-weight demo

This runner uses the existing camera detector, active product catalogue,
`CartSession` billing operations, SQLite session log, and Firebase session
projection. Operator weights are marked `manual_demo`; it does not read, emulate,
or claim an HX711 connection. Every submitted input gets a unique event ID;
replaying an ID cannot bill a second unit. A successful action consumes that
visible detection episode until the product leaves the frame.

The latest available database backups map the products to product IDs 6, 2, 1,
4, and 5 respectively. Their existing catalogue prices are ₹10, ₹5, ₹10, ₹10,
and ₹10. The application reads prices from the active database; demo weights
below are operator inputs and do not overwrite catalogue weights or prices.

## Required configuration

Use the same existing Firebase project, service-account path, active database,
customer pairing, and secure API tunnel for both processes. Keep service-account
credentials outside this repository. Set the actual values in each terminal:

```sh
export SMARTCART_FIREBASE_URL='https://<existing-project>-default-rtdb.firebaseio.com'
export GOOGLE_APPLICATION_CREDENTIALS='/absolute/path/to/protected-service-account.json'
export SMARTCART_DATABASE_PATH="$PWD/data/smartcart_demo.db"
export SMARTCART_API_BASE_URL='https://<existing-secure-tunnel-host>'
export PYTHON='/Users/Ankush/Desktop/smart-cart/.venv/bin/python'
```

The existing catalogue must already be installed and contain the five model
classes. A customer must pair with the cart in the phone app before the camera
runner starts. The phone's `SMARTCART_API_BASE_URL` must point to the same
existing HTTPS tunnel used for the API; do not replace it with a guessed or
insecure endpoint.

## Start services

Terminal 1, from the repository root:

```sh
export SMARTCART_FIREBASE_URL='https://<existing-project>-default-rtdb.firebaseio.com'
export GOOGLE_APPLICATION_CREDENTIALS='/absolute/path/to/protected-service-account.json'
export SMARTCART_DATABASE_PATH="$PWD/data/smartcart_demo.db"
$PYTHON -m edge.api.run_server
```

Keep the project's already configured secure tunnel running to this API. Use
the same environment values and database path in Terminal 2:

```sh
export SMARTCART_FIREBASE_URL='https://<existing-project>-default-rtdb.firebaseio.com'
export GOOGLE_APPLICATION_CREDENTIALS='/absolute/path/to/protected-service-account.json'
export SMARTCART_DATABASE_PATH="$PWD/data/smartcart_demo.db"
$PYTHON -m demo.manual_camera_weight_demo --cart-id CART-001
```

If the model is stored elsewhere, supply its existing path with
`--model /absolute/path/to/best.pt`. The model file is intentionally not tracked.

## Build and install Android debug APK

Place the existing project's `google-services.json` at
`app/android/app/google-services.json` (keep it untracked), and use the same
Firebase project as the authenticated application. Then run:

```sh
cd app
/Users/Ankush/flutter/bin/flutter pub get
/Users/Ankush/flutter/bin/flutter build apk --debug \
  --dart-define=SMARTCART_API_BASE_URL="$SMARTCART_API_BASE_URL"
adb install -r build/app/outputs/flutter-apk/app-debug.apk
```

Open the app, sign in as the customer, and pair it to the active cart. Its
existing authenticated Firebase subscription displays the backend's `bill_items`
and `total_amount` updates; the camera program does not change Flutter state.

## Operator input

For each new camera detection, select the catalogue SKU by number or name, then
choose ADD or REMOVE. Enter the exact signed demo weight. The supported inputs
are:

| SKU | Add | Remove |
|---|---:|---:|
| LIFEBUOY | `+40` | `-40` |
| MAGGI_5 | `+30` | `-30` |
| MAGGI_10 | `+57` | `-57` |
| COCA_COLA | `+256` | `-256` |
| LAYS_25G | `+25` | `-25` |

For example, choose `LIFEBUOY`, choose ADD, and enter `+40`. To remove it,
choose `LIFEBUOY`, choose REMOVE, and enter `-40`. The console reports the
event ID and source for accepted events and the reason for rejected ones.
Invalid selections, actions, and weights are rejected; removing an item absent
from the cart is rejected by the shared billing path. Ctrl-C in the terminal or
`q` in the camera window exits cleanly.

## Known limitations

- This is a manual-weight demonstration, not a physical load-cell validation.
- A model file, active catalogue, Firebase credentials, active paired session,
  and secure phone-to-backend network path must already be configured.
- Camera detection accuracy and live Firebase/device behavior depend on the
  existing model and project configuration and must be checked on the demo setup.
- The phone remains authenticated through the existing Firebase Auth flow; the
  backend cannot make an unauthenticated customer subscription work.
