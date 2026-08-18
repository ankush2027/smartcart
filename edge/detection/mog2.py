import cv2


camera = cv2.VideoCapture(0)

if not camera.isOpened():
    print("Could not open camera")
    exit()


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

while True:
    success, frame = camera.read()

    if not success:
        print("Could not read frame")
        break

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

    contours, _ = cv2.findContours(
    mask,
    cv2.RETR_EXTERNAL,
    cv2.CHAIN_APPROX_SIMPLE
    )

    for contour in contours:
        area = cv2.contourArea(contour)

        if area > 500:
            x, y, w, h = cv2.boundingRect(contour)

            cv2.rectangle(
                frame,
                (x, y),
                (x + w, y + h),
                (0, 255, 0),
                2
            )

    foreground_pixels = cv2.countNonZero(mask)

    if foreground_pixels > 1000:
        movement_frames += 1
    else:
        movement_frames = 0

    if movement_frames >= 5:
        print("Confirmed movement")

    cv2.imshow("Original", frame)
    cv2.imshow("Clean MOG2 Mask", mask)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break


camera.release()
cv2.destroyAllWindows()