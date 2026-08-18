import cv2
from camera import open_camera
from event import create_movement_event

camera = open_camera()

mog2 = cv2.createBackgroundSubtractorMOG2(
    history=500,
    varThreshold=50,
    detectShadows=True
)

kernel = cv2.getStructuringElement(
    cv2.MORPH_ELLIPSE,
    (5, 5)
)

movement_frames = 0
movement_active = False

# Number of consecutive frames required to confirm movement
REQUIRED_FRAMES = 5

while True:
    success, frame = camera.read()

    if not success:
        print("Could not read frame")
        break

    # -----------------------------
    # MOG2 background subtraction
    # -----------------------------
    mask = mog2.apply(frame)

    # Remove small noise
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        kernel
    )

    # Fill small gaps
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        kernel
    )

    # Calculate foreground pixels BEFORE using them
    foreground_pixels = cv2.countNonZero(mask)

    # -----------------------------
    # Find significant movement
    # -----------------------------
    largest_contour = None
    largest_area = 0

    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    for contour in contours:

        area = cv2.contourArea(contour)

        if area > largest_area:
            largest_area = area
            largest_contour = contour

    # -----------------------------
    # Movement detection
    # -----------------------------
    if largest_area > 500:

        movement_frames += 1

        x, y, w, h = cv2.boundingRect(largest_contour)

        cv2.rectangle(
            frame,
            (x, y),
            (x + w, y + h),
            (0, 255, 0),
            2
        )

    else:
        movement_frames = 0

        # Movement has stopped
        movement_active = False

    # -----------------------------
    # Confirm movement ONCE
    # -----------------------------
    if movement_frames >= REQUIRED_FRAMES:

        if not movement_active:

            movement_active = True

            event = create_movement_event(
                (x, y, w, h),
                foreground_pixels
            )

            print(event)
            print("Confirmed movement")

    # -----------------------------
    # Display
    # -----------------------------
    cv2.imshow("Original", frame)
    cv2.imshow("Clean MOG2 Mask", mask)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break


camera.release()
cv2.destroyAllWindows()