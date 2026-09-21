# pyrefly: ignore [missing-import]
import cv2
from edge.detection.camera import open_camera
from edge.detection.event import create_movement_event

# Number of consecutive frames required to confirm movement
REQUIRED_FRAMES = 5


def run_motion_detector(camera_index: int = 0) -> None:
    """
    Open the camera and run the MOG2 motion-detection loop.

    Prints a DetectionEvent to stdout each time confirmed movement is
    detected.  Press 'q' in either display window to quit.

    Parameters
    ----------
    camera_index:
        OpenCV camera index (default 0 = built-in/first webcam).
    """
    camera = open_camera(camera_index)

    mog2 = cv2.createBackgroundSubtractorMOG2(
        history=500,
        varThreshold=50,
        detectShadows=True,
    )

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    movement_frames = 0
    movement_active = False

    while True:
        success, frame = camera.read()

        if not success:
            print("Could not read frame")
            break

        # -----------------------------------------------------------------
        # MOG2 background subtraction
        # -----------------------------------------------------------------
        mask = mog2.apply(frame)

        # Remove small noise
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        # Fill small gaps
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        foreground_pixels = cv2.countNonZero(mask)

        # -----------------------------------------------------------------
        # Find significant movement
        # -----------------------------------------------------------------
        largest_contour = None
        largest_area = 0

        contours, _ = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )

        for contour in contours:
            area = cv2.contourArea(contour)
            if area > largest_area:
                largest_area = area
                largest_contour = contour

        # -----------------------------------------------------------------
        # Movement detection
        # -----------------------------------------------------------------
        if largest_area > 500:
            movement_frames += 1

            x, y, w, h = cv2.boundingRect(largest_contour)
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)

        else:
            movement_frames = 0
            movement_active = False

        # -----------------------------------------------------------------
        # Confirm movement ONCE per gesture
        # -----------------------------------------------------------------
        if movement_frames >= REQUIRED_FRAMES:
            if not movement_active:
                movement_active = True

                event = create_movement_event((x, y, w, h), foreground_pixels)
                print(event)
                print("Confirmed movement")

        # -----------------------------------------------------------------
        # Display
        # -----------------------------------------------------------------
        cv2.imshow("Original", frame)
        cv2.imshow("Clean MOG2 Mask", mask)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    camera.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    run_motion_detector()