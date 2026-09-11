import socket
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from local_web_server.models import HostApp, HostRegistry
from local_web_server.service_ports import ServicePortAllocator, ServicePortError


@contextmanager
def bound_loopback_port(port: int = 0):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def registry_with_ports(*ports: int) -> HostRegistry:
    apps = tuple(
        HostApp(
            id=f"service-{index}",
            repository=Path(f"/tmp/service-{index}"),
            auto_deploy=True,
            environment_file=None,
            environment=(),
            port=port,
            start_command=None,
        )
        for index, port in enumerate(ports)
    )
    return HostRegistry("system-index.local", Path("/tmp/runtime"), apps)


class ServicePortAllocatorTests(unittest.TestCase):
    def test_selects_the_first_port_not_registered_or_bound_on_loopback(self):
        with bound_loopback_port() as bound_port:
            allocator = ServicePortAllocator(bound_port - 1, bound_port + 1)

            selected = allocator.select(registry_with_ports(bound_port - 1))

        self.assertEqual(selected, bound_port + 1)

    def test_excluded_ports_support_bounded_reselection(self):
        allocator = ServicePortAllocator(52000, 52002, available=lambda _port: True)

        selected = allocator.select(
            registry_with_ports(), excluded=frozenset({52000, 52001})
        )

        self.assertEqual(selected, 52002)

    def test_exhaustion_and_invalid_ranges_fail_without_unbounded_search(self):
        checked = []
        allocator = ServicePortAllocator(
            52000,
            52002,
            available=lambda port: checked.append(port) or False,
        )

        with self.assertRaisesRegex(ServicePortError, "no service port is available"):
            allocator.select(registry_with_ports())
        self.assertEqual(checked, [52000, 52001, 52002])

        for first, last in ((0, 5), (80, 80), (53000, 52000), (1, 70000)):
            with self.subTest(first=first, last=last):
                with self.assertRaises(ServicePortError):
                    ServicePortAllocator(first, last)


if __name__ == "__main__":
    unittest.main()
