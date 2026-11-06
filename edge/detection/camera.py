import cv2


def open_camera(camera_index=0):
    camera = cv2.VideoCapture(camera_index)

    if not camera.isOpened():
        raise RuntimeError("Could not open camera")

    return camera