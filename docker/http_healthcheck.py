"""Bounded, childless HTTP healthcheck for the web containers."""

import http.client
import signal
import sys


def _deadline_expired(_signum, _frame):
    raise TimeoutError("healthcheck deadline exceeded")


def main(port=8000):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    previous_handler = signal.signal(signal.SIGALRM, _deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        connection.request("GET", "/healthz/")
        return 0 if connection.getresponse().status == 200 else 1
    except (OSError, http.client.HTTPException):
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
