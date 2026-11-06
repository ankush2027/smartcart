class WeightSensor:
    """
    Simulated weight sensor for laptop testing.

    Later this class can be replaced with
    the real HX711 implementation on Raspberry Pi.
    """

    def __init__(self):
        self.current_weight = 500.0

    def read_weight(self):
        """Return the current simulated weight in grams."""
        return self.current_weight

    def simulate_change(self, change):
        """Simulate adding or removing weight."""
        self.current_weight += change

        if self.current_weight < 0:
            self.current_weight = 0

        return self.current_weight


def main():
    sensor = WeightSensor()

    print("Initial weight:", sensor.read_weight(), "g")

    print("\nAdding 75g...")
    sensor.simulate_change(75)
    print("Current weight:", sensor.read_weight(), "g")

    print("\nRemoving 75g...")
    sensor.simulate_change(-75)
    print("Current weight:", sensor.read_weight(), "g")


if __name__ == "__main__":
    main()