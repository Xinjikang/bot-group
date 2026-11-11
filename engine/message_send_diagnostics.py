"""TEMP_SEND_TRACE: test instrumentation. Remove after send stalls are diagnosed."""

import logging
import re
import time


logger = logging.getLogger(__name__)
TRACE_QUERY = re.compile(rb'send_trace=([0-9a-f]{32})')


class MessageSendDiagnostics:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        match = None
        if scope['type'] == 'http' and scope['method'] == 'POST' and scope['path'] == '/msg':
            match = TRACE_QUERY.fullmatch(scope.get('query_string', b''))
        if match is None:
            return await self.app(scope, receive, send)

        started = time.perf_counter()
        trace = {'id': match[1].decode('ascii')}
        scope.setdefault('state', {})['message_send_trace'] = trace
        logger.info('test.message_send.request trace_id=%s', trace['id'])
        status = headers_at = body_at = None

        async def timed_send(message):
            nonlocal status, headers_at, body_at
            await send(message)
            if message['type'] == 'http.response.start':
                status = message['status']
                headers_at = time.perf_counter()
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                body_at = time.perf_counter()

        def elapsed(timestamp):
            return '-' if timestamp is None else f'{(timestamp - started) * 1000:.1f}'

        try:
            await self.app(scope, receive, timed_send)
        finally:
            logger.info('test.message_send.response trace_id=%s status=%s pre_handler_ms=%s '
                        'headers_ms=%s response_ms=%s total_ms=%s', trace['id'], status,
                        elapsed(trace.get('handler_at')), elapsed(headers_at), elapsed(body_at),
                        elapsed(time.perf_counter()))
