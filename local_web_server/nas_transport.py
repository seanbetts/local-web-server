"""Bounded fixed-action JSON transport to a private installation-owned adapter."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time
from typing import Any

from .nas_models import NasTarget
from .process_runner import _stop_process_group


_ACTIONS = frozenset({'prepare', 'upload', 'activate', 'status', 'rollback', 'finish'})
_OPERATION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')


class NasTransportError(ValueError):
    """A failed request retains identity so callers can reconcile with status."""

    def __init__(self, action: str, operation_id: str, *, outcome_unknown: bool):
        self.action = action
        self.operation_id = operation_id
        self.outcome_unknown = outcome_unknown
        super().__init__('NAS adapter acknowledgement unavailable' if outcome_unknown
                         else 'NAS adapter request failed')


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate JSON field')
        result[name] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError('invalid JSON constant')


class NasTransport:
    """Run exactly one direct-argv adapter request; mutations are never retried.

    stdout is a single bounded acknowledgement, stderr is discarded. Large blob
    and image bytes travel outside this protocol in the adapter's upload bundle.
    A lost activation acknowledgement must be reconciled by a status request
    carrying the original operation ID, never by replaying activate.
    """

    def __init__(self, target: NasTarget, *, timeout_seconds: float = 30,
                 maximum_json_bytes: int = 1024 * 1024):
        if (target.kind != 'nas-docker' or type(target.adapter_command) is not tuple
                or not target.adapter_command
                or any(type(arg) is not str or not arg or '\0' in arg for arg in target.adapter_command)
                or not Path(target.adapter_command[0]).is_absolute()):
            raise ValueError('NAS target requires an absolute direct-argument adapter command')
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0 or type(maximum_json_bytes) is not int
                or not 128 <= maximum_json_bytes <= 16 * 1024 * 1024):
            raise ValueError('invalid NAS adapter execution bounds')
        self.target = target
        self.timeout_seconds = timeout_seconds
        self.maximum_json_bytes = maximum_json_bytes

    def request(self, action: str, *, operation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if (type(action) is not str or action not in _ACTIONS
                or type(operation_id) is not str or not _OPERATION.fullmatch(operation_id)
                or type(payload) is not dict):
            raise ValueError('invalid NAS adapter request')
        envelope = dict(schemaVersion=1, action=action, operationId=operation_id, payload=payload)
        try:
            request = json.dumps(envelope, separators=(',', ':'), ensure_ascii=False,
                                 allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeError, RecursionError) as error:
            raise ValueError('NAS request must contain JSON metadata') from error
        if len(request) > self.maximum_json_bytes:
            raise ValueError('NAS request metadata exceeds the configured bound')
        response = self._exchange(request, action, operation_id)
        try:
            acknowledgement = json.loads(response.decode('utf-8'), object_pairs_hook=_unique_object,
                                          parse_constant=_reject_constant)
            if (type(acknowledgement) is not dict
                    or type(acknowledgement.get('schemaVersion')) is not int
                    or acknowledgement['schemaVersion'] != 1
                    or acknowledgement.get('action') != action
                    or acknowledgement.get('operationId') != operation_id
                    or type(acknowledgement.get('ok')) is not bool):
                raise ValueError('invalid NAS acknowledgement')
            if acknowledgement['ok']:
                if set(acknowledgement) != {'schemaVersion', 'action', 'operationId', 'ok', 'result'} or type(acknowledgement['result']) is not dict:
                    raise ValueError('invalid NAS success acknowledgement')
                return acknowledgement['result']
            error_keys = {'schemaVersion', 'action', 'operationId', 'ok', 'error'}
            if (set(acknowledgement) not in (error_keys, error_keys | {'outcomeUnknown'})
                    or type(acknowledgement['error']) is not str
                    or ('outcomeUnknown' in acknowledgement and type(acknowledgement['outcomeUnknown']) is not bool)):
                raise ValueError('invalid NAS rejection acknowledgement')
        except (ValueError, UnicodeError, RecursionError) as error:
            raise NasTransportError(action, operation_id, outcome_unknown=True) from error
        raise NasTransportError(action, operation_id, outcome_unknown=acknowledgement.get('outcomeUnknown', False))

    def _exchange(self, request: bytes, action: str, operation_id: str) -> bytes:
        environment = dict(PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
                           HOME=str(Path.home()), LANG='C', LC_ALL='C',
                           GIT_TERMINAL_PROMPT='0')
        # Retain the existing agent route, without copying unrelated app secrets.
        if 'SSH_AUTH_SOCK' in os.environ:
            environment['SSH_AUTH_SOCK'] = os.environ['SSH_AUTH_SOCK']
        with tempfile.TemporaryDirectory(prefix='local-web-nas-adapter-') as temporary:
            try:
                process = subprocess.Popen(self.target.adapter_command, cwd=temporary,
                    env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                raise NasTransportError(action, operation_id, outcome_unknown=False) from error
            if process.stdin is None or process.stdout is None:
                _stop_process_group(process, .5, .01)
                raise NasTransportError(action, operation_id, outcome_unknown=True)
            selector = selectors.DefaultSelector()
            deadline = time.monotonic() + self.timeout_seconds
            response = bytearray()
            sent = 0
            try:
                os.set_blocking(process.stdin.fileno(), False)
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    for key, _ in selector.select(min(remaining, .1)):
                        if key.fileobj is process.stdin:
                            try:
                                sent += os.write(process.stdin.fileno(), request[sent:sent + 65536])
                            except BrokenPipeError:
                                selector.unregister(process.stdin)
                                process.stdin.close()
                                continue
                            if sent == len(request):
                                selector.unregister(process.stdin)
                                process.stdin.close()
                        else:
                            chunk = os.read(process.stdout.fileno(), min(65536, self.maximum_json_bytes + 1 - len(response)))
                            if not chunk:
                                selector.unregister(process.stdout)
                            else:
                                response.extend(chunk)
                                if len(response) > self.maximum_json_bytes:
                                    raise ValueError('NAS response exceeds the configured bound')
                remaining = deadline - time.monotonic()
                if remaining <= 0 or process.wait(timeout=remaining) != 0 or sent != len(request):
                    raise ValueError('NAS adapter process failed')
                return bytes(response)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise NasTransportError(action, operation_id, outcome_unknown=True) from error
            finally:
                selector.close()
                try:
                    try:
                        _stop_process_group(process, .5, .01)
                    except ValueError as error:
                        raise NasTransportError(action, operation_id, outcome_unknown=True) from error
                finally:
                    process.stdin.close()
                    process.stdout.close()
