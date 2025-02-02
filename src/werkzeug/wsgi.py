import io
import re
import typing as t
import warnings
from functools import partial
from functools import update_wrapper
from itertools import chain

from ._internal import _make_encode_wrapper
from ._internal import _to_bytes
from ._internal import _to_str
from .exceptions import ClientDisconnected
from .exceptions import RequestEntityTooLarge
from .sansio import utils as _sansio_utils
from .sansio.utils import host_is_trusted  # noqa: F401 # Imported as part of API

if t.TYPE_CHECKING:
    from _typeshed.wsgi import WSGIApplication
    from _typeshed.wsgi import WSGIEnvironment


def responder(f: t.Callable[..., "WSGIApplication"]) -> "WSGIApplication":
    """Marks a function as responder.  Decorate a function with it and it
    will automatically call the return value as WSGI application.

    Example::

        @responder
        def application(environ, start_response):
            return Response('Hello World!')
    """
    return update_wrapper(lambda *a: f(*a)(*a[-2:]), f)


def get_current_url(
    environ: "WSGIEnvironment",
    root_only: bool = False,
    strip_querystring: bool = False,
    host_only: bool = False,
    trusted_hosts: t.Optional[t.Iterable[str]] = None,
) -> str:
    """Recreate the URL for a request from the parts in a WSGI
    environment.

    The URL is an IRI, not a URI, so it may contain Unicode characters.
    Use :func:`~werkzeug.urls.iri_to_uri` to convert it to ASCII.

    :param environ: The WSGI environment to get the URL parts from.
    :param root_only: Only build the root path, don't include the
        remaining path or query string.
    :param strip_querystring: Don't include the query string.
    :param host_only: Only build the scheme and host.
    :param trusted_hosts: A list of trusted host names to validate the
        host against.
    """
    parts = {
        "scheme": environ["wsgi.url_scheme"],
        "host": get_host(environ, trusted_hosts),
    }

    if not host_only:
        parts["root_path"] = environ.get("SCRIPT_NAME", "")

        if not root_only:
            parts["path"] = environ.get("PATH_INFO", "")

            if not strip_querystring:
                parts["query_string"] = environ.get("QUERY_STRING", "").encode("latin1")

    return _sansio_utils.get_current_url(**parts)


def _get_server(
    environ: "WSGIEnvironment",
) -> t.Optional[t.Tuple[str, t.Optional[int]]]:
    name = environ.get("SERVER_NAME")

    if name is None:
        return None

    try:
        port: t.Optional[int] = int(environ.get("SERVER_PORT", None))
    except (TypeError, ValueError):
        # unix socket
        port = None

    return name, port


def get_host(
    environ: "WSGIEnvironment", trusted_hosts: t.Optional[t.Iterable[str]] = None
) -> str:
    """Return the host for the given WSGI environment.

    The ``Host`` header is preferred, then ``SERVER_NAME`` if it's not
    set. The returned host will only contain the port if it is different
    than the standard port for the protocol.

    Optionally, verify that the host is trusted using
    :func:`host_is_trusted` and raise a
    :exc:`~werkzeug.exceptions.SecurityError` if it is not.

    :param environ: A WSGI environment dict.
    :param trusted_hosts: A list of trusted host names.

    :return: Host, with port if necessary.
    :raise ~werkzeug.exceptions.SecurityError: If the host is not
        trusted.
    """
    return _sansio_utils.get_host(
        environ["wsgi.url_scheme"],
        environ.get("HTTP_HOST"),
        _get_server(environ),
        trusted_hosts,
    )


def get_content_length(environ: "WSGIEnvironment") -> t.Optional[int]:
    """Return the ``Content-Length`` header value as an int. If the header is not given
    or the ``Transfer-Encoding`` header is ``chunked``, ``None`` is returned to indicate
    a streaming request. If the value is not an integer, or negative, 0 is returned.

    :param environ: The WSGI environ to get the content length from.

    .. versionadded:: 0.9
    """
    return _sansio_utils.get_content_length(
        http_content_length=environ.get("CONTENT_LENGTH"),
        http_transfer_encoding=environ.get("HTTP_TRANSFER_ENCODING"),
    )


def get_input_stream(
    environ: "WSGIEnvironment",
    safe_fallback: bool = True,
    max_content_length: t.Optional[int] = None,
) -> t.IO[bytes]:
    """Return the WSGI input stream, wrapped so that it may be read safely without going
    past the ``Content-Length`` header value or ``max_content_length``.

    If ``Content-Length`` exceeds ``max_content_length``, a
    :exc:`RequestEntityTooLarge`` ``413 Content Too Large`` error is raised.

    If the WSGI server sets ``environ["wsgi.input_terminated"]``, it indicates that the
    server handles terminating the stream, so it is safe to read directly. For example,
    a server that knows how to handle chunked requests safely would set this.

    If ``max_content_length`` is set, that limit is used even if ``Content-Length`` or
    ``wsgi.input_terminated`` are set. If none of these are set, then an empty stream is
    returned unless the user explicitly disables this safe fallback.

    If the limit is reached before the underlying stream is exhausted (such as a file
    that is too large, or an infinite stream), the remaining contents of the stream
    cannot be read safely. Depending on how the server handles this, clients may show a
    "connection reset" failure instead of seeing the 413 response.

    :param environ: The WSGI environ containing the stream.
    :param safe_fallback: Return an empty stream when ``Content-Length`` is not set.
        Disabling this allows infinite streams, which can be a denial-of-service risk.
    :param max_content_length: The maximum length that content-length or streaming
        requests may not exceed.

    .. versionchanged:: 2.3
        Check ``max_content_length`` and raise an error if it is exceeded.

    .. versionadded:: 0.9
    """
    stream = t.cast(t.IO[bytes], environ["wsgi.input"])
    content_length = get_content_length(environ)

    if content_length is not None and max_content_length is not None:
        if content_length > max_content_length:
            raise RequestEntityTooLarge()
    elif max_content_length is not None:
        return t.cast(
            t.IO[bytes], LimitedStream(stream, max_content_length, is_max=True)
        )

    # A WSGI server can set this to indicate that it terminates the input stream. In
    # that case the stream is safe without wrapping.
    if "wsgi.input_terminated" in environ:
        return stream

    # No limit given, return an empty stream unless the user explicitly allows the
    # potentially infinite stream. An infinite stream is dangerous if it's not expected,
    # as it can tie up a worker indefinitely.
    if content_length is None:
        return io.BytesIO() if safe_fallback else stream

    return t.cast(t.IO[bytes], LimitedStream(stream, content_length))


def get_path_info(
    environ: "WSGIEnvironment",
    charset: t.Any = ...,
    errors: t.Optional[str] = None,
) -> str:
    """Return ``PATH_INFO`` from  the WSGI environment.

    :param environ: WSGI environment to get the path from.

    .. versionchanged:: 2.3
        The ``charset`` and ``errors`` parameters are deprecated and will be removed in
        Werkzeug 2.4.

    .. versionadded:: 0.9
    """
    if charset is not ...:
        warnings.warn(
            "The 'charset' parameter is deprecated and will be removed"
            " in Werkzeug 2.4.",
            DeprecationWarning,
            stacklevel=2,
        )

        if charset is None:
            charset = "utf-8"
    else:
        charset = "utf-8"

    if errors is not None:
        warnings.warn(
            "The 'errors' parameter is deprecated and will be removed in Werkzeug 2.4",
            DeprecationWarning,
            stacklevel=2,
        )
    else:
        errors = "replace"

    path = environ.get("PATH_INFO", "").encode("latin1")
    return path.decode(charset, errors)  # type: ignore[no-any-return]


class ClosingIterator:
    """The WSGI specification requires that all middlewares and gateways
    respect the `close` callback of the iterable returned by the application.
    Because it is useful to add another close action to a returned iterable
    and adding a custom iterable is a boring task this class can be used for
    that::

        return ClosingIterator(app(environ, start_response), [cleanup_session,
                                                              cleanup_locals])

    If there is just one close function it can be passed instead of the list.

    A closing iterator is not needed if the application uses response objects
    and finishes the processing if the response is started::

        try:
            return response(environ, start_response)
        finally:
            cleanup_session()
            cleanup_locals()
    """

    def __init__(
        self,
        iterable: t.Iterable[bytes],
        callbacks: t.Optional[
            t.Union[t.Callable[[], None], t.Iterable[t.Callable[[], None]]]
        ] = None,
    ) -> None:
        iterator = iter(iterable)
        self._next = t.cast(t.Callable[[], bytes], partial(next, iterator))
        if callbacks is None:
            callbacks = []
        elif callable(callbacks):
            callbacks = [callbacks]
        else:
            callbacks = list(callbacks)
        iterable_close = getattr(iterable, "close", None)
        if iterable_close:
            callbacks.insert(0, iterable_close)
        self._callbacks = callbacks

    def __iter__(self) -> "ClosingIterator":
        return self

    def __next__(self) -> bytes:
        return self._next()

    def close(self) -> None:
        for callback in self._callbacks:
            callback()


def wrap_file(
    environ: "WSGIEnvironment", file: t.IO[bytes], buffer_size: int = 8192
) -> t.Iterable[bytes]:
    """Wraps a file.  This uses the WSGI server's file wrapper if available
    or otherwise the generic :class:`FileWrapper`.

    .. versionadded:: 0.5

    If the file wrapper from the WSGI server is used it's important to not
    iterate over it from inside the application but to pass it through
    unchanged.  If you want to pass out a file wrapper inside a response
    object you have to set :attr:`Response.direct_passthrough` to `True`.

    More information about file wrappers are available in :pep:`333`.

    :param file: a :class:`file`-like object with a :meth:`~file.read` method.
    :param buffer_size: number of bytes for one iteration.
    """
    return environ.get("wsgi.file_wrapper", FileWrapper)(  # type: ignore
        file, buffer_size
    )


class FileWrapper:
    """This class can be used to convert a :class:`file`-like object into
    an iterable.  It yields `buffer_size` blocks until the file is fully
    read.

    You should not use this class directly but rather use the
    :func:`wrap_file` function that uses the WSGI server's file wrapper
    support if it's available.

    .. versionadded:: 0.5

    If you're using this object together with a :class:`Response` you have
    to use the `direct_passthrough` mode.

    :param file: a :class:`file`-like object with a :meth:`~file.read` method.
    :param buffer_size: number of bytes for one iteration.
    """

    def __init__(self, file: t.IO[bytes], buffer_size: int = 8192) -> None:
        self.file = file
        self.buffer_size = buffer_size

    def close(self) -> None:
        if hasattr(self.file, "close"):
            self.file.close()

    def seekable(self) -> bool:
        if hasattr(self.file, "seekable"):
            return self.file.seekable()
        if hasattr(self.file, "seek"):
            return True
        return False

    def seek(self, *args: t.Any) -> None:
        if hasattr(self.file, "seek"):
            self.file.seek(*args)

    def tell(self) -> t.Optional[int]:
        if hasattr(self.file, "tell"):
            return self.file.tell()
        return None

    def __iter__(self) -> "FileWrapper":
        return self

    def __next__(self) -> bytes:
        data = self.file.read(self.buffer_size)
        if data:
            return data
        raise StopIteration()


class _RangeWrapper:
    # private for now, but should we make it public in the future ?

    """This class can be used to convert an iterable object into
    an iterable that will only yield a piece of the underlying content.
    It yields blocks until the underlying stream range is fully read.
    The yielded blocks will have a size that can't exceed the original
    iterator defined block size, but that can be smaller.

    If you're using this object together with a :class:`Response` you have
    to use the `direct_passthrough` mode.

    :param iterable: an iterable object with a :meth:`__next__` method.
    :param start_byte: byte from which read will start.
    :param byte_range: how many bytes to read.
    """

    def __init__(
        self,
        iterable: t.Union[t.Iterable[bytes], t.IO[bytes]],
        start_byte: int = 0,
        byte_range: t.Optional[int] = None,
    ):
        self.iterable = iter(iterable)
        self.byte_range = byte_range
        self.start_byte = start_byte
        self.end_byte = None

        if byte_range is not None:
            self.end_byte = start_byte + byte_range

        self.read_length = 0
        self.seekable = hasattr(iterable, "seekable") and iterable.seekable()
        self.end_reached = False

    def __iter__(self) -> "_RangeWrapper":
        return self

    def _next_chunk(self) -> bytes:
        try:
            chunk = next(self.iterable)
            self.read_length += len(chunk)
            return chunk
        except StopIteration:
            self.end_reached = True
            raise

    def _first_iteration(self) -> t.Tuple[t.Optional[bytes], int]:
        chunk = None
        if self.seekable:
            self.iterable.seek(self.start_byte)  # type: ignore
            self.read_length = self.iterable.tell()  # type: ignore
            contextual_read_length = self.read_length
        else:
            while self.read_length <= self.start_byte:
                chunk = self._next_chunk()
            if chunk is not None:
                chunk = chunk[self.start_byte - self.read_length :]
            contextual_read_length = self.start_byte
        return chunk, contextual_read_length

    def _next(self) -> bytes:
        if self.end_reached:
            raise StopIteration()
        chunk = None
        contextual_read_length = self.read_length
        if self.read_length == 0:
            chunk, contextual_read_length = self._first_iteration()
        if chunk is None:
            chunk = self._next_chunk()
        if self.end_byte is not None and self.read_length >= self.end_byte:
            self.end_reached = True
            return chunk[: self.end_byte - contextual_read_length]
        return chunk

    def __next__(self) -> bytes:
        chunk = self._next()
        if chunk:
            return chunk
        self.end_reached = True
        raise StopIteration()

    def close(self) -> None:
        if hasattr(self.iterable, "close"):
            self.iterable.close()


def _make_chunk_iter(
    stream: t.Union[t.Iterable[bytes], t.IO[bytes]],
    limit: t.Optional[int],
    buffer_size: int,
) -> t.Iterator[bytes]:
    """Helper for the line and chunk iter functions."""
    warnings.warn(
        "'_make_chunk_iter' is deprecated and will be removed in Werkzeug 2.4.",
        DeprecationWarning,
        stacklevel=2,
    )

    if isinstance(stream, (bytes, bytearray, str)):
        raise TypeError(
            "Passed a string or byte object instead of true iterator or stream."
        )
    if not hasattr(stream, "read"):
        for item in stream:
            if item:
                yield item
        return
    stream = t.cast(t.IO[bytes], stream)
    if not isinstance(stream, LimitedStream) and limit is not None:
        stream = t.cast(t.IO[bytes], LimitedStream(stream, limit))
    _read = stream.read
    while True:
        item = _read(buffer_size)
        if not item:
            break
        yield item


def make_line_iter(
    stream: t.Union[t.Iterable[bytes], t.IO[bytes]],
    limit: t.Optional[int] = None,
    buffer_size: int = 10 * 1024,
    cap_at_buffer: bool = False,
) -> t.Iterator[bytes]:
    """Safely iterates line-based over an input stream.  If the input stream
    is not a :class:`LimitedStream` the `limit` parameter is mandatory.

    This uses the stream's :meth:`~file.read` method internally as opposite
    to the :meth:`~file.readline` method that is unsafe and can only be used
    in violation of the WSGI specification.  The same problem applies to the
    `__iter__` function of the input stream which calls :meth:`~file.readline`
    without arguments.

    If you need line-by-line processing it's strongly recommended to iterate
    over the input stream using this helper function.

    .. deprecated:: 2.3
        Will be removed in Werkzeug 2.4.

    .. versionadded:: 0.11
       added support for the `cap_at_buffer` parameter.

    .. versionadded:: 0.9
       added support for iterators as input stream.

    .. versionchanged:: 0.8
       This function now ensures that the limit was reached.

    :param stream: the stream or iterate to iterate over.
    :param limit: the limit in bytes for the stream.  (Usually
                  content length.  Not necessary if the `stream`
                  is a :class:`LimitedStream`.
    :param buffer_size: The optional buffer size.
    :param cap_at_buffer: if this is set chunks are split if they are longer
                          than the buffer size.  Internally this is implemented
                          that the buffer size might be exhausted by a factor
                          of two however.
    """
    warnings.warn(
        "'make_line_iter' is deprecated and will be removed in Werkzeug 2.4.",
        DeprecationWarning,
        stacklevel=2,
    )
    _iter = _make_chunk_iter(stream, limit, buffer_size)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "'_make_chunk_iter", DeprecationWarning)
        first_item = next(_iter, "")

    if not first_item:
        return

    s = _make_encode_wrapper(first_item)
    empty = t.cast(bytes, s(""))
    cr = t.cast(bytes, s("\r"))
    lf = t.cast(bytes, s("\n"))
    crlf = t.cast(bytes, s("\r\n"))

    _iter = t.cast(t.Iterator[bytes], chain((first_item,), _iter))

    def _iter_basic_lines() -> t.Iterator[bytes]:
        _join = empty.join
        buffer: t.List[bytes] = []
        while True:
            new_data = next(_iter, "")
            if not new_data:
                break
            new_buf: t.List[bytes] = []
            buf_size = 0
            for item in t.cast(
                t.Iterator[bytes], chain(buffer, new_data.splitlines(True))
            ):
                new_buf.append(item)
                buf_size += len(item)
                if item and item[-1:] in crlf:
                    yield _join(new_buf)
                    new_buf = []
                elif cap_at_buffer and buf_size >= buffer_size:
                    rv = _join(new_buf)
                    while len(rv) >= buffer_size:
                        yield rv[:buffer_size]
                        rv = rv[buffer_size:]
                    new_buf = [rv]
            buffer = new_buf
        if buffer:
            yield _join(buffer)

    # This hackery is necessary to merge 'foo\r' and '\n' into one item
    # of 'foo\r\n' if we were unlucky and we hit a chunk boundary.
    previous = empty
    for item in _iter_basic_lines():
        if item == lf and previous[-1:] == cr:
            previous += item
            item = empty
        if previous:
            yield previous
        previous = item
    if previous:
        yield previous


def make_chunk_iter(
    stream: t.Union[t.Iterable[bytes], t.IO[bytes]],
    separator: bytes,
    limit: t.Optional[int] = None,
    buffer_size: int = 10 * 1024,
    cap_at_buffer: bool = False,
) -> t.Iterator[bytes]:
    """Works like :func:`make_line_iter` but accepts a separator
    which divides chunks.  If you want newline based processing
    you should use :func:`make_line_iter` instead as it
    supports arbitrary newline markers.

    .. deprecated:: 2.3
        Will be removed in Werkzeug 2.4.

    .. versionchanged:: 0.11
       added support for the `cap_at_buffer` parameter.

    .. versionchanged:: 0.9
       added support for iterators as input stream.

    .. versionadded:: 0.8

    :param stream: the stream or iterate to iterate over.
    :param separator: the separator that divides chunks.
    :param limit: the limit in bytes for the stream.  (Usually
                  content length.  Not necessary if the `stream`
                  is otherwise already limited).
    :param buffer_size: The optional buffer size.
    :param cap_at_buffer: if this is set chunks are split if they are longer
                          than the buffer size.  Internally this is implemented
                          that the buffer size might be exhausted by a factor
                          of two however.
    """
    warnings.warn(
        "'make_chunk_iter' is deprecated and will be removed in Werkzeug 2.4.",
        DeprecationWarning,
        stacklevel=2,
    )
    _iter = _make_chunk_iter(stream, limit, buffer_size)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "'_make_chunk_iter", DeprecationWarning)
        first_item = next(_iter, b"")

    if not first_item:
        return

    _iter = t.cast(t.Iterator[bytes], chain((first_item,), _iter))
    if isinstance(first_item, str):
        separator = _to_str(separator)
        _split = re.compile(f"({re.escape(separator)})").split
        _join = "".join
    else:
        separator = _to_bytes(separator)
        _split = re.compile(b"(" + re.escape(separator) + b")").split
        _join = b"".join

    buffer: t.List[bytes] = []
    while True:
        new_data = next(_iter, b"")
        if not new_data:
            break
        chunks = _split(new_data)
        new_buf: t.List[bytes] = []
        buf_size = 0
        for item in chain(buffer, chunks):
            if item == separator:
                yield _join(new_buf)
                new_buf = []
                buf_size = 0
            else:
                buf_size += len(item)
                new_buf.append(item)

                if cap_at_buffer and buf_size >= buffer_size:
                    rv = _join(new_buf)
                    while len(rv) >= buffer_size:
                        yield rv[:buffer_size]
                        rv = rv[buffer_size:]
                    new_buf = [rv]
                    buf_size = len(rv)

        buffer = new_buf
    if buffer:
        yield _join(buffer)


class LimitedStream(io.RawIOBase):
    """Wrap a stream so that it doesn't read more than a given limit. This is used to
    limit ``wsgi.input`` to the ``Content-Length`` header value or
    :attr:`.Request.max_content_length`.

    When attempting to read after the limit has been reached, :meth:`on_exhausted` is
    called. When the limit is a maximum, this raises :exc:`.RequestEntityTooLarge`.

    If reading from the stream returns zero bytes or raises an error,
    :meth:`on_disconnect` is called, which raises :exc:`.ClientDisconnected`. When the
    limit is a maximum and zero bytes were read, no error is raised, since it may be the
    end of the stream.

    If the limit is reached before the underlying stream is exhausted (such as a file
    that is too large, or an infinite stream), the remaining contents of the stream
    cannot be read safely. Depending on how the server handles this, clients may show a
    "connection reset" failure instead of seeing the 413 response.

    :param stream: The stream to read from. Must be a readable binary IO object.
    :param limit: The limit in bytes to not read past. Should be either the
        ``Content-Length`` header value or ``request.max_content_length``.
    :param is_max: Whether the given ``limit`` is ``request.max_content_length`` instead
        of the ``Content-Length`` header value. This changes how exhausted and
        disconnect events are handled.

    .. versionchanged:: 2.3
        Handle ``max_content_length`` differently than ``Content-Length``.

    .. versionchanged:: 2.3
        Implements ``io.RawIOBase`` rather than ``io.IOBase``.
    """

    def __init__(self, stream: t.IO[bytes], limit: int, is_max: bool = False) -> None:
        self._stream = stream
        self._pos = 0
        self.limit = limit
        self._limit_is_max = is_max

    @property
    def is_exhausted(self) -> bool:
        """Whether the current stream position has reached the limit."""
        return self._pos >= self.limit

    def on_exhausted(self) -> None:
        """Called when attempting to read after the limit has been reached.

        The default behavior is to do nothing, unless the limit is a maximum, in which
        case it raises :exc:`.RequestEntityTooLarge`.

        .. versionchanged:: 2.3
            Raises ``RequestEntityTooLarge`` if the limit is a maximum.

        .. versionchanged:: 2.3
            Any return value is ignored.
        """
        if self._limit_is_max:
            raise RequestEntityTooLarge()

    def on_disconnect(self, error: t.Optional[Exception] = None) -> None:
        """Called when an attempted read receives zero bytes before the limit was
        reached. This indicates that the client disconnected before sending the full
        request body.

        The default behavior is to raise :exc:`.ClientDisconnected`, unless the limit is
        a maximum and no error was raised.

        .. versionchanged:: 2.3
            Added the ``error`` parameter. Do nothing if the limit is a maximum and no
            error was raised.

        .. versionchanged:: 2.3
            Any return value is ignored.
        """
        if not self._limit_is_max or error is not None:
            raise ClientDisconnected()

        # If the limit is a maximum, then we may have read zero bytes because the
        # streaming body is complete. There's no way to distinguish that from the
        # client disconnecting early.

    def exhaust(self) -> bytes:
        """Exhaust the stream by reading until the limit is reached or the client
        disconnects, returning the remaining data.

        .. versionchanged:: 2.3
            Return the remaining data.

        .. versionchanged:: 2.2.3
            Handle case where wrapped stream returns fewer bytes than requested.
        """
        if not self.is_exhausted:
            return self.readall()

        return b""

    def readinto(self, b: bytearray) -> t.Optional[int]:  # type: ignore[override]
        size = len(b)
        remaining = self.limit - self._pos

        if remaining <= 0:
            self.on_exhausted()
            return 0

        if hasattr(self._stream, "readinto"):
            # Use stream.readinto if it's available.
            if size <= remaining:
                # The size fits in the remaining limit, use the buffer directly.
                try:
                    out_size: t.Optional[int] = self._stream.readinto(b)
                except (OSError, ValueError) as e:
                    self.on_disconnect(error=e)
                    return 0
            else:
                # Use a temp buffer with the remaining limit as the size.
                temp_b = bytearray(remaining)

                try:
                    out_size = self._stream.readinto(temp_b)
                except (OSError, ValueError) as e:
                    self.on_disconnect(error=e)
                    return 0

                if out_size:
                    b[:out_size] = temp_b
        else:
            # WSGI requires that stream.read is available.
            try:
                data = self._stream.read(min(size, remaining))
            except (OSError, ValueError) as e:
                self.on_disconnect(error=e)
                return 0

            out_size = len(data)
            b[:out_size] = data

        if not out_size:
            # Read zero bytes from the stream.
            self.on_disconnect()
            return 0

        self._pos += out_size
        return out_size

    def readall(self) -> bytes:
        if self.is_exhausted:
            self.on_exhausted()
            return b""

        out = bytearray()

        # The parent implementation uses "while True", which results in an extra read.
        while not self.is_exhausted:
            data = self.read(1024 * 64)

            # Stream may return empty before a max limit is reached.
            if not data:
                break

            out.extend(data)

        return bytes(out)

    def tell(self) -> int:
        """Return the current stream position.

        .. versionadded:: 0.9
        """
        return self._pos

    def readable(self) -> bool:
        return True
