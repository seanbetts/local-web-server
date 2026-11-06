import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from local_web_server.nas_models import NasTarget
from local_web_server.nas_transport import NasTransport, NasTransportError


class NasTransportTests(unittest.TestCase):
    def adapter(self, root, body):
        path = root / 'adapter.py'
        path.write_text('import json, os, sys, time\nfrom pathlib import Path\n'
                        'request = json.load(sys.stdin)\n' + body)
        return NasTarget(kind='nas-docker', adapter_command=(sys.executable, str(path)))

    def test_six_fixed_actions_use_json_envelopes_and_direct_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.adapter(root,
                "json.dump(dict(schemaVersion=1, action=request['action'], operationId=request['operationId'], "
                "ok=True, result=request['payload']), sys.stdout)\n")
            transport = NasTransport(target)
            for action in ('prepare', 'upload', 'activate', 'status', 'rollback', 'finish'):
                payload = dict(appId='example', value='$(touch never); literal')
                result = transport.request(action, operation_id='operation-1', payload=payload)
                self.assertEqual(result, payload)
            self.assertFalse((root / 'never').exists())
            for action in ('exec', 'shell', '', 'activate; rm'):
                with self.assertRaises(ValueError):
                    transport.request(action, operation_id='operation-1', payload={})

    def test_lost_activation_ack_requires_status_and_does_not_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'activations'
            target = self.adapter(root,
                "state = Path(" + repr(str(state)) + ")\n"
                "if request['action'] == 'activate':\n"
                "    with state.open('a') as stream: stream.write(request['operationId'] + '\\n')\n"
                "    sys.exit(0)\n"
                "json.dump(dict(schemaVersion=1, action=request['action'], operationId=request['operationId'], "
                "ok=True, result=dict(activated=state.read_text().splitlines())), sys.stdout)\n")
            transport = NasTransport(target)
            with self.assertRaises(NasTransportError) as caught:
                transport.request('activate', operation_id='operation-1', payload={})
            self.assertEqual(caught.exception.operation_id, 'operation-1')
            self.assertEqual(caught.exception.action, 'activate')
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertEqual(transport.request('status', operation_id='operation-1', payload={}),
                             dict(activated=['operation-1']))
            self.assertEqual(state.read_text(), 'operation-1\n')

    def test_rejection_is_known_and_never_echoes_private_adapter_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.adapter(Path(temporary),
                "json.dump(dict(schemaVersion=1, action=request['action'], operationId=request['operationId'], "
                "ok=False, error='SECRET token private host'), sys.stdout)\n")
            with self.assertRaises(NasTransportError) as caught:
                NasTransport(target).request('prepare', operation_id='op', payload={})
            self.assertFalse(caught.exception.outcome_unknown)
            self.assertNotIn('SECRET', str(caught.exception))

    def test_mismatched_duplicate_malformed_or_oversize_response_is_unknown(self):
        scripts = (
            "print('invalid SECRET')",
            "json.dump(dict(schemaVersion=1,action='status',operationId='wrong',ok=True,result={}),sys.stdout)",
            "print('{\"schemaVersion\":1,\"schemaVersion\":1,\"action\":\"status\",\"operationId\":\"op\",\"ok\":true,\"result\":{}}')",
            "sys.stdout.write('x' * 10000)",
        )
        with tempfile.TemporaryDirectory() as temporary:
            for script in scripts:
                with self.subTest(script=script):
                    target = self.adapter(Path(temporary), script + '\n')
                    with self.assertRaises(NasTransportError) as caught:
                        NasTransport(target, maximum_json_bytes=512).request('status', operation_id='op', payload={})
                    self.assertTrue(caught.exception.outcome_unknown)
                    self.assertNotIn('SECRET', str(caught.exception))

    def test_timeout_is_bounded_and_request_limit_runs_no_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / 'invoked'
            target = self.adapter(root,
                "Path(" + repr(str(marker)) + ").write_text('yes')\ntime.sleep(10)\n")
            transport = NasTransport(target, timeout_seconds=.1, maximum_json_bytes=512)
            started = time.monotonic()
            with self.assertRaises(NasTransportError) as caught:
                transport.request('activate', operation_id='op', payload={})
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertLess(time.monotonic() - started, 2)
            marker.unlink()
            with self.assertRaises(ValueError):
                transport.request('upload', operation_id='op', payload={'data': 'x' * 1000})
            self.assertFalse(marker.exists())
            with self.assertRaises(ValueError):
                transport.request('prepare', operation_id='../bad', payload={})

    def test_unavailable_adapter_is_known_not_started(self):
        target = NasTarget(kind='nas-docker', adapter_command=('/nonexistent/local-web-adapter',))
        with self.assertRaises(NasTransportError) as caught:
            NasTransport(target).request('prepare', operation_id='op', payload={})
        self.assertFalse(caught.exception.outcome_unknown)

    def test_explicit_rejection_uncertainty_is_typed_and_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            for flag in (True, False, 'invalid'):
                target = self.adapter(Path(temporary),
                    "json.dump(dict(schemaVersion=1, action=request['action'], operationId=request['operationId'], "
                    + "ok=False, error='private', outcomeUnknown=" + repr(flag) + "), sys.stdout)\n")
                with self.subTest(flag=flag), self.assertRaises(NasTransportError) as caught:
                    NasTransport(target).request('activate', operation_id='original', payload={})
                self.assertIs(caught.exception.outcome_unknown, flag if type(flag) is bool else True)


if __name__ == '__main__':
    unittest.main()
