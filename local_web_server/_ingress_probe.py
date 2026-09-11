"""One HTTPS HEAD exchange, run only in a deadline-controlled child process."""

import http.client
import ssl
import sys


def request_status(host: str, timeout: float) -> int:
    connection = http.client.HTTPSConnection(
        host, 443, timeout=timeout, context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "HEAD", "/_local-web/platform/index/registry-v1.json", headers={"Host": host},
        )
        response = connection.getresponse()
        try:
            return response.status
        finally:
            response.close()
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        status = request_status(sys.argv[1], float(sys.argv[2]))
    except (OSError, http.client.HTTPException, ValueError):
        sys.exit(1)
    sys.stdout.write(str(status))
