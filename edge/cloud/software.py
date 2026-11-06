"""Local operator-only simulation attached to the secured cloud composition."""
from edge.demo.software_input import SoftwareConsole, SoftwareDemoRuntime
from edge.runtime.supervisor import CartSupervisor


def create_software_supervisor(publisher):
    console = SoftwareConsole()
    console.output('SOFTWARE DEMO: synthetic product recognition and weights; no physical hardware validation.')

    class SerializedRuntime(SoftwareDemoRuntime):
        def process(self, kind, label):
            # Serialize direct runtime projections with the cloud outbox publisher.
            with publisher.lock:
                return super().process(kind, label)

    def factory(session_id):
        with publisher.lock:
            return SerializedRuntime(cart_id='CART-001', session_id=session_id,
                                     firebase_ref=publisher.ref, console=console)

    return CartSupervisor(cart_id='CART-001', runtime_factory=factory,
                          use_cart_lock=True), console
