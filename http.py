import asyncio
import traceback
from urllib.parse import urlencode


MAX_RESPONSE_SIZE = 100000000


class HttpError(Exception):
    '''
    General HTTP exception.
    '''
    pass

class ProxyError(Exception):
    '''
    This exception means: use other proxy.
    '''
    pass

class ResponseTooLargeError(Exception):
    '''
    Zip bomb, probably.
    '''
    pass


_http_headers = {
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection':      'keep-alive',
    'Sec-Fetch-Dest':  'document',
    'Sec-Fetch-Mode':  'navigate',
    'Sec-Fetch-Site':  'cross-site',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0'
}

_session_defaults = dict(
    proxy = None,
    headers = _http_headers,
    connect_timeout = 30
)


def create_http_session(**kwargs):
    '''
    How to use:

        async with create_http_session() as session:
            response = await session.get(url)
            headers = response.headers
            content = response.content
    '''
    # aiohttp does not support HTTP/2
    #
    # How to use:
    #
    #    async with create_http_session(config) as session:
    #        async with session.get(url) as response:
    #            content = await response.read()
    #
    # connector = aiohttp_socks.ProxyConnector(
    #     proxy_type = aiohttp_socks.ProxyType.SOCKS5,
    #     host = proxy['address'],
    #     port = proxy['socks_port'],
    #     rdns = True
    # )
    # timeout = aiohttp.ClientTimeout(total=config.http['timeout'])
    # return aiohttp.ClientSession(
    #     connector = connector,
    #     headers = {**_http_headers, **headers},
    #     timeout = timeout
    # )

    return CurlHttpSession(**kwargs)


##################################################
# CURL-based implementation
#
# cloudflare uses TLS fingerprinting. Use CURL compiled with BoringSSL:
# https://everything.curl.dev/build/tls/boringssl

import pycurl
import certifi
from io import BytesIO


# global instance for now, should be per-thread but threads aren't used anyway
_curl_multi = pycurl.CurlMulti()

_curl_timeout_task = None
_curl_fds = set()
_requests = dict()  # request objects by easy handle

def _curl_socket_action(sock_fd, ev_bitmask):
    '''
    read/write available data given an action or handle timeout
    '''
    status, num_running_handles = _curl_multi.socket_action(sock_fd, ev_bitmask)

    if num_running_handles != len(_requests):
        loop = asyncio.get_running_loop()
        while True:
            num_queued, success_handles, failed_handles = _curl_multi.info_read()

            for handle in success_handles:
                request = _requests.pop(handle)
                loop.call_soon(request.success)

            for handle, errno, errmsg in failed_handles:
                request = _requests.pop(handle)
                loop.call_soon(request.failure, errno, errmsg)

            if num_queued == 0:
                break

def _curl_socket_function(ev_bitmask, sock_fd, multi, data):
    '''
    callback informed about what to wait for
    '''
    loop = asyncio.get_running_loop()

    if sock_fd in _curl_fds:
        loop.remove_reader(sock_fd)
        loop.remove_writer(sock_fd)

    if ev_bitmask & pycurl.POLL_IN:
        loop.add_reader(sock_fd, _curl_socket_action, sock_fd, pycurl.CSELECT_IN)
        _curl_fds.add(sock_fd)

    if ev_bitmask & pycurl.POLL_OUT:
        loop.add_writer(sock_fd, _curl_socket_action, sock_fd, pycurl.CSELECT_OUT)
        _curl_fds.add(sock_fd)

    if ev_bitmask & pycurl.POLL_REMOVE:
        _curl_fds.remove(sock_fd)

def _curl_timer_function(timeout_ms):
    '''
    callback to receive timeout values
    '''
    global _curl_timeout_task

    if _curl_timeout_task:
        _curl_timeout_task.cancel()

    if timeout_ms == -1:
        _curl_timeout_task = None
    else:
        loop = asyncio.get_running_loop()
        _curl_timeout_task = loop.call_later(timeout_ms / 1000, _curl_socket_action, pycurl.SOCKET_TIMEOUT, 0)

_curl_multi.setopt(_curl_multi.M_SOCKETFUNCTION, _curl_socket_function)
_curl_multi.setopt(_curl_multi.M_TIMERFUNCTION, _curl_timer_function)


_possible_proxy_errors = set([
    pycurl.E_COULDNT_RESOLVE_PROXY,
    pycurl.E_COULDNT_RESOLVE_HOST,
    pycurl.E_COULDNT_CONNECT,
    pycurl.E_SSL_CONNECT_ERROR,
    pycurl.E_SSL_CERTPROBLEM,
    pycurl.E_PEER_FAILED_VERIFICATION,
    97 # CURLE_PROXY
])


# use pool of easy handles instead of creating them for each requestt
_easy_handles = []

def acquire_easy_handle():
    # get handle frompool or create new one
    try:
        handle = _easy_handles.pop()
        handle.reset()
    except IndexError:
        handle = pycurl.Curl()
    return handle

def release_easy_handle(handle):
    # return handle to the pool
    _easy_handles.append(handle)


class CurlHttpSession:

    def __init__(self, proxies=None, **session_params):
        self.proxies = proxies
        self.proxy_index = 0
        if proxies:
            self.proxy = proxies[0]
        else:
            self.proxy = None
        self.session_params = session_params

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        pass

    async def head(self, url, **kwargs):
        response = await self._request(url, 'HEAD', **self._make_request_params(**kwargs))
        return response

    async def get(self, url, **kwargs):
        response = await self._request(url, 'GET', **self._make_request_params(**kwargs))
        return response

    async def post(self, url, form_data=None, post_data=None, **kwargs):
        response = await self._request(
            url, 'POST',
            form_data = form_data,
            post_data = post_data,
            **self._make_request_params(**kwargs)
        )
        return response

    def _make_request_params(self, **kwargs):
        # combine _session_defaults, self.session_params, and kwargs
        result = dict()
        for params in (_session_defaults, self.session_params, kwargs):
            for k, v in params.items():
                if isinstance(v, dict) and k in result:
                    # XXX this is for headers only, isn't it?
                    result[k] |= v
                else:
                    result[k] = v
        if self.proxy is not None:
            result['proxy'] = self.proxy
        return result

    async def _request(self, url, method, **kwargs):
        request = CurlHttpRequest(url, method, **kwargs)
        response = await request.perform()
        return response

    async def next_proxy(self, wait=20):
        # use next tor proxy
        # XXX revise this
        if len(self.proxies) == 0:
            return
        self.proxy_index += 1
        if self.proxy_index >= len(self.proxies):
            self.proxy_index = 0
            if wait:
                print(f'Tried all proxies, sleeping {wait}s before next round')
                await asyncio.sleep(wait)
        self.proxy = self.proxies[self.proxy_index]
        print('using next proxy:', self.proxy)

    @property
    def waysout(self):
        # XXX fucking ugly, revise
        return range(len(self.proxies) or 1)


class CurlHttpRequest:

    def __init__(self, url, method, headers=None, proxy=None, connect_timeout=None, debug=None,
                 post_data=None, form_data=None):

        # post_data, form_data - use one of

        self.url = url
        self.method = method
        self.easy_handle = None
        self.response_body = BytesIO()
        self.response_body_size = 0

        self.easy_handle = c = acquire_easy_handle()

        c.setopt(c.URL, url)
        c.setopt(c.HTTP_VERSION, c.CURL_HTTP_VERSION_2_0)
        c.setopt(c.CAINFO, certifi.where())
        c.setopt(c.ACCEPT_ENCODING, 'gzip, deflate, br')
        c.setopt(c.FOLLOWLOCATION, 1)

        if headers:
            c.setopt(c.HTTPHEADER, list('{0}: {1}'.format(k, v) for k, v in headers.items()))

        if proxy is not None:
            c.setopt(c.PROXY, proxy)

        if connect_timeout is not None:
            c.setopt(c.CONNECTTIMEOUT, connect_timeout)

        if debug:
            c.setopt(c.VERBOSE, 1)

        if method == 'POST':
            if form_data is not None:
                post_data = urlencode(form_data)
            c.setopt(c.POSTFIELDS, post_data)

        self.response = CurlHttpResponse()
        self.header_expect = 'status'
        c.setopt(c.HEADERFUNCTION, self.header_function)

        c.setopt(c.WRITEFUNCTION, self.write_response_body)

        self.waiter = None

    def __del__(self):
        self.close()

    def write_response_body(self, data):
        self.response_body_size += len(data)
        if self.response_body_size > MAX_RESPONSE_SIZE:
            raise ResponseTooLargeError()
        self.response_body.write(data)

    def close(self):
        self.response_body.close()
        if self.easy_handle is not None:
            try:
                _curl_multi.remove_handle(self.easy_handle)
                _requests.pop(self.easy_handle, None)
            except Exception:
                traceback.print_exc()
            release_easy_handle(self.easy_handle)
            self.easy_handle = None

    def perform(self):

        if self.waiter is not None:
            raise RuntimeError('Cannot perform already performing request')

        _curl_multi.add_handle(self.easy_handle)
        _requests[self.easy_handle] = self

        loop = asyncio.get_running_loop()
        self.waiter = loop.create_future()
        return self.waiter

    def success(self):
        # called from _curl_socket_action
        if self.waiter is None:
            raise RuntimeError('Not performing this request')

        self.response.content = self.response_body.getvalue()
        self.response.real_url = self.easy_handle.getinfo(pycurl.EFFECTIVE_URL)
        if not self.waiter.cancelled():
            self.waiter.set_result(self.response)
        self.waiter = None
        self.close()

    def failure(self, errno, errmsg):
        # called from _curl_socket_action
        if self.waiter is None:
            raise RuntimeError('Not performing this request')

        if not self.waiter.cancelled():
            if errno in _possible_proxy_errors:
                self.waiter.set_exception(ProxyError(self.url, errno, errmsg))
            else:
                self.waiter.set_exception(HttpError(self.url, errno, errmsg))
        self.waiter = None
        self.close()

    def header_function(self, header_line):
        # HTTP standard specifies that headers are encoded in iso-8859-1.
        header_line = header_line.decode('iso-8859-1')

        if self.header_expect == 'status':
            # parse status line
            http, status, *reason = header_line.split(' ', maxsplit=2)
            self.response.version = http.split('/', maxsplit=1)[-1]
            self.response.status = status
            if len(reason):
                self.response.reason = reason[0].strip()

            if self.response.headers is not None:
                self.response.prev_headers.append(self.response.headers)
            self.response.headers = []

            self.header_expect = 'name:value'
            return

        if self.header_expect == 'name:value':
            if header_line[0] in ' \t':
                # value continued
                if len(self.response.headers) == 0:
                    print('XXX malformed header line:', repr(header_line))
                    return
                # update last value
                name, value = self.response.headers.pop()
                value = f'{value} {header_line.strip()}'
                self.response.headers.append((name, value))
                return

            if header_line.strip() == '':
                # last empty line
                self.header_expect = 'status'
                return

            if ':' not in header_line:
                print('XXX malformed header line:', repr(header_line))
                return

            name, value = header_line.split(':', maxsplit=1)
            name = name.strip().lower()
            value = value.strip()
            self.response.headers.append((name, value))
            return

        print('XXX', self.url, 'unexpected header line:', repr(header_line))

class CurlHttpResponse:

    def __init__(self):
        self.version = None
        self.status = None
        self.reason = ''
        self.prev_headers = []  # list of previous redirect headers
        self.headers = None
        self.real_url = None
        self.content = None
