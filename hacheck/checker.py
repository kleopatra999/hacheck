import datetime
import socket
import time

import tornado.concurrent
import tornado.ioloop
import tornado.iostream
import tornado.gen
import tornado.httpclient

from . import cache
from . import config
from . import mysql
from . import spool
from . import __version__

TIMEOUT = 10


# Do not cache spool checks
@tornado.concurrent.return_future
def check_spool(service_name, port, query, io_loop, callback, query_params, headers):
    up, extra_info = spool.is_up(service_name, port=port)
    if not up:
        info_string = 'Service %s in down state' % (extra_info['service'],)
        if extra_info.get('creation') is not None:
            info_string += ' since %f' % extra_info['creation']
        if extra_info.get('expiration') is not None:
            info_string += ' until %f' % extra_info['expiration']
        if extra_info.get('reason', ''):
            info_string += ": %s" % extra_info['reason']
        callback((503, info_string))
    else:
        callback((200, extra_info.get('reason', '')))


# IMPORTANT: the gen.coroutine decorator needs to be the innermost
@cache.cached
@tornado.gen.coroutine
def check_http(service_name, port, check_path, io_loop, query_params, headers):
    return check_http_https(service_name, port, check_path, io_loop, query_params, headers, False)


# IMPORTANT: the gen.coroutine decorator needs to be the innermost
@cache.cached
@tornado.gen.coroutine
def check_https(service_name, port, check_path, io_loop, query_params, headers):
    return check_http_https(service_name, port, check_path, io_loop, query_params, headers, True)


def check_http_https(service_name, port, check_path, io_loop, query_params, headers, ssl):
    if ssl:
        method = "https"
    else:
        method = "http"
    qp = query_params
    if not check_path.startswith("/"):
        check_path = "/" + check_path  # pragma: no cover
    headers_out = {'User-Agent': 'hastate %s' % (__version__)}
    for header in config.config['http_headers_to_copy']:
        if header in headers:
            headers_out[header] = headers[header]
    if config.config['service_name_header']:
        headers_out[config.config['service_name_header']] = service_name
    path = '%s://127.0.0.1:%d%s%s' % (method, port, check_path, '?' + qp if qp else '')
    request = tornado.httpclient.HTTPRequest(
        path,
        method='GET',
        headers=headers_out,
        request_timeout=TIMEOUT,
        follow_redirects=False,
        validate_cert=False,
    )
    http_client = tornado.httpclient.AsyncHTTPClient(io_loop=io_loop)
    try:
        response = yield http_client.fetch(request)
        code = response.code
        reason = response.body
    except tornado.httpclient.HTTPError as exc:
        code = exc.code
        reason = exc.response.body if exc.response else ""
    except Exception as e:
        code = 599
        reason = 'Unhandled exception %s' % e

    # Some necessary house-keeping
    del http_client
    raise tornado.gen.Return((code, reason))


@cache.cached
@tornado.gen.coroutine
def check_tcp(service_name, port, query, io_loop, query_params, headers):
    stream = None
    connect_start = time.time()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    try:
        stream = tornado.iostream.IOStream(s, io_loop=io_loop)
        yield tornado.gen.with_timeout(
            datetime.timedelta(seconds=TIMEOUT),
            tornado.gen.Task(stream.connect, ('127.0.0.1', port))
        )

    except tornado.gen.TimeoutError:
        raise tornado.gen.Return((
            503,
            'Connection timed out after %.2fs' % (time.time() - connect_start)
        ))
    except socket.error as e:
        raise tornado.gen.Return((
            503,
            'Unexpected error %s after %2fs' % (e, time.time() - connect_start)
        ))
    finally:
        if stream:
            stream.close()
        # Some necessary house-keeping
        del stream
    raise tornado.gen.Return((
        200,
        'Connected in %.2fs' % (time.time() - connect_start)
    ))


@cache.cached
@tornado.gen.coroutine
def check_mysql(service_name, port, query, io_loop, query_params, headers):
    username = config.config.get('mysql_username', None)
    password = config.config.get('mysql_password', None)
    if username is None or password is None:
        raise tornado.gen.Return((500, 'No MySQL username/pasword in config file'))

    def timed_out(duration):
        raise tornado.gen.Return((503, 'MySQL timed out after %.2fs' % (duration)))

    conn = mysql.MySQLClient(port=port, global_timeout=TIMEOUT, io_loop=io_loop)
    response = yield conn.connect(username, password)
    if not response.OK:
        raise tornado.gen.Return((500, 'MySQL sez %s' % response))
    yield conn.quit()
    raise tornado.gen.Return((200, 'MySQL connect response: %s' % response))


@cache.cached
@tornado.gen.coroutine
def check_smtp(service_name, port, query, io_loop, query_params, headers):
    stream = None
    connect_start = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    try:
        stream = tornado.iostream.IOStream(s, io_loop=io_loop)
        yield tornado.gen.with_timeout(
            datetime.timedelta(seconds=TIMEOUT),
            tornado.gen.Task(
                stream.connect, ('127.0.0.1', port))
        )
        yield stream.read_until(b'\r\n')
        yield stream.write(b'QUIT\r\n')
        quit_response = yield stream.read_until(b'\r\n')
        if quit_response.decode('utf-8').split()[0] != '221':
            raise tornado.gen.Return((503, 'Got unexpected QUIT response {0!r}'.format(quit_response)))
    except tornado.gen.TimeoutError:
        raise tornado.gen.Return((
            503,
            'Connection timed out after %.2fs' % (time.time() - connect_start)
        ))
    except tornado.iostream.StreamClosedError:
        raise tornado.gen.Return((503, 'Peer unexpectedly closed connection'))
    except socket.error as e:
        raise tornado.gen.Return((
            503,
            'Unexpected socket error %s after %2fs' % (e.errno, time.time() - connect_start)
        ))
    finally:
        if stream:
            stream.close()
        # Some necessary house-keeping
        del stream
    raise tornado.gen.Return((
        200,
        'Connected in %.2fs' % (time.time() - connect_start)
    ))
