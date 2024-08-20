"""`resizewin` detects the terminal size.

This is a pure Python implementation of the terminal size detection,
originally implemented by the ``resize`` command (part of the X11 ``xterm``)
and later ported to FreeBSD as ``resizewin``.

In order to detect the terminal size,
`resizewin` first tells the terminal to move the cursor to as far as possible,
then asks the terminal for the current cursor's position.
It uses the fact that terminals move the cursor to the bottom right corner
when told to move the cursor to a row/column position that exceeds screen size.

The core terminal size detection logic is captured in `get_terminal_size()`:

>>> fd = sys.stdin.fileno()
>>> rows, cols = get_terminal_size(fd)
>>> termios.tcsetwinsize(fd, (rows, cols))
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import sys
import termios
from argparse import ArgumentParser
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from typing import NamedTuple, Optional, Self

__version__ = '0.1.0'

_logger = logging.getLogger(__name__)

_has_tcsetwinsize = getattr(termios, 'tcsetwinsize', None) is not None
_has_tcgetwinsize = getattr(termios, 'tcgetwinsize', None) is not None


class TTYAttributes(NamedTuple):
    """Terminal attributes.

    It is a simple named tuple that wraps the list returned/expected
    by `termios.tcgetattr()`/`termios.tcsetattr()` respectively.
    """
    iflag: int
    oflag: int
    cflag: int
    lflag: int
    ispeed: int
    ospeed: int
    cc: list[bytes | int]

    def patch(self,
              iflag: Optional[Iterable[int]] = None,
              oflag: Optional[Iterable[int]] = None,
              cflag: Optional[Iterable[int]] = None,
              lflag: Optional[Iterable[int]] = None,
              ispeed: Optional[int] = None,
              ospeed: Optional[int] = None,
              cc: Optional[Mapping[int, bytes | int]] = None) -> Self:
        """Patch terminal attributes.

        Flag patches are given as an iterable of int,
        either positive (to set) or negative (to clear),
        e.g. termios.ICANON or ~termios.ISTRIP.
        Bitwise-OR positive patches; bitwise-AND negatives.

        Leave `self` intact; return a copy with modified attributes.

        Example to enable 8-bit canonical mode and set Ctrl-C as interrupt key:

        >>> # noinspection PyShadowingNames
        >>> before = TTYAttributes(*termios.tcgetattr(sys.stdin))
        >>> after = before.patch(iflag=[~termios.ISTRIP],
        ...                      lflag=[termios.ICANON],
        ...                      cc={termios.VINTR: b'\003'})
        >>> assert after.iflag & termios.ISTRIP == 0
        >>> assert after.lflag & termios.ICANON == termios.ICANON
        >>> assert after.cc[termios.VINTR] == b'\003'

        To patch multi-bit flags, clear its mask first then set bits,
        e.g. in order to set character size to 8 bits:

        >>> after = before.patch(cflag=[~termios.CSIZE, termios.CS8])
        >>> assert after.cflag & termios.CSIZE == termios.CS8

        :param iflag: iflags to modify.
        :param oflag: oflags to modify.
        :param cflag: cflags to modify.
        :param lflag: lflags to modify.
        :param ispeed: new ispeed.
        :param ospeed: new ospeed.
        :param cc: new control characters.
        :return: a modified copy of terminal attributes.
        """

        before = self._replace(cc=list(self.cc))

        class _Changes(dict):
            def __missing__(self, k):
                v = getattr(before, k)
                self[k] = v
                return v

        changes = _Changes()
        for field, flags in dict(iflag=iflag, oflag=oflag,
                                 cflag=cflag, lflag=lflag).items():
            for flag in flags or ():
                if flag >= 0:
                    changes[field] |= flag
                else:
                    changes[field] &= flag
        for field, value in dict(ispeed=ispeed, ospeed=ospeed).items():
            if value is not None:
                changes[field] = value
        for key, value in (cc or {}).items():
            changes['cc'][key] = value
        return self._replace(**changes)


def _fd_when(fd: Optional[int], when: Optional[int]) -> tuple[int, int]:
    if fd is None:
        fd = sys.stdin.fileno()
    if when is None:
        when = termios.TCSAFLUSH
    return fd, when


@contextmanager
def tcattr_saved(
        fd: Optional[int] = None, when: int = None,
) -> Iterator[TTYAttributes]:
    """Save/restore terminal attributes upon enter/exit.

    :param fd: the terminal; if `None` (default), use `sys.stdin`.
    :param when: when to restore attributes.  See `termios.tcgetattr()`.
    :return: (a context manager that yields) the current attributes.
    """
    fd, when = _fd_when(fd, when)
    attrs = TTYAttributes(*termios.tcgetattr(fd))
    try:
        yield attrs
    finally:
        termios.tcsetattr(fd, when, list(attrs))


RAW_PATCH = dict(
    iflag={~termios.IGNBRK, ~termios.BRKINT, ~termios.IGNPAR,
           ~termios.PARMRK, ~termios.INPCK, ~termios.ISTRIP,
           ~termios.INLCR, ~termios.IGNCR, ~termios.ICRNL,
           ~termios.IXON, ~termios.IXANY, ~termios.IXOFF,
           ~termios.IUCLC, ~termios.IMAXBEL},
    oflag={~termios.OPOST},
    lflag={~termios.ISIG, ~termios.ICANON, ~termios.XCASE},
    cc={termios.VMIN: 1, termios.VTIME: 0},
)
"""Raw terminal attributes, usable as kwargs of `TTYAttribute.patch()`."""

_SETUP_PATCH = dict(
    lflag=[~termios.ECHO],
    cc={termios.VMIN: 0, termios.VTIME: 1},
)


@contextmanager
def _setup_terminal(
        fd: int, when: int,
) -> Iterator[None]:
    with tcattr_saved(fd, when) as attrs:
        attrs = attrs.patch(**RAW_PATCH).patch(**_SETUP_PATCH)
        termios.tcsetattr(fd, when, list(attrs))
        yield None


def _write_full(fd: int, data: bytes) -> None:
    while data:
        num_written = os.write(fd, data)
        data = data[num_written:]


_pos_re = re.compile(b'\033' br'\[(\d+);(\d+)R')


def _seek_pos(fd: int, row: int, col: int) -> None:
    _write_full(fd, (b'\033[' +
                     b';'.join(str(v).encode() for v in (row, col)) +
                     b'H'))


def _read_pos(fd: int) -> tuple[int, int]:
    data = b''
    while True:
        chunk = os.read(fd, 1024)
        if not chunk:
            msg = "timed out while reading cursor position from terminal"
            raise RuntimeError(msg)
        data += chunk
        m = _pos_re.search(data)
        if m is not None:
            return int(m.group(1)), int(m.group(2))


def _query_pos(fd: int) -> tuple[int, int]:
    _write_full(fd, b'\033[6n')
    return _read_pos(fd)


def get_terminal_size(
        fd: Optional[int] = None,
        when: Optional[int] = None,
) -> tuple[int, int]:
    """Detect the terminal size.

    :param fd: the terminal; if `None` (default), use `sys.stdin`.
    :param when: when to restore attributes.  See `termios.tcgetattr()`.
    :return: the number of rows and columns.
    """
    fd, when = _fd_when(fd, when)
    with _setup_terminal(fd, when):
        orig_row, orig_col = _query_pos(fd)
        seek_row, seek_col = 1, 1
        rows, cols = None, None
        while rows is None or cols is None:
            _seek_pos(fd, seek_row, seek_col)
            row, col = _query_pos(fd)
            if row < seek_row:
                rows = row
            else:
                seek_row *= 2
            if col < seek_col:
                cols = col
            else:
                seek_col *= 2
        _seek_pos(fd, orig_row, orig_col)
    return rows, cols


class ShellType(Enum):
    """Type of shell.

    Used as a CLI ``--shell`` argument"""
    BOURNE = 'sh'
    C = 'csh'


def main():
    parser = ArgumentParser()
    shell_action = parser.add_mutually_exclusive_group()
    shell_default_help = "none" if _has_tcsetwinsize else "autodetect"
    shell_action.add_argument('-u', '--sh', '--bash', action='store_const',
                              dest='shell', const=ShellType.BOURNE,
                              help="""emit Bourne shell/Bash export commands
                                      (equivalent to --shell sh)""")
    shell_action.add_argument('-c', '--csh', '--tcsh', action='store_const',
                              dest='shell', const=ShellType.C,
                              help="""emit C shell setenv commands
                                      (equivalent to --shell csh)""")
    shell_action.add_argument('--shell', metavar='TYPE', type=ShellType,
                              help=f"""emit commands for shell TYPE (one of:
                                       {', '.join(t.value
                                                  for t in ShellType)};
                                       default: {shell_default_help})""")
    if _has_tcgetwinsize:
        parser.add_argument('-z', '--only-if-unset', action='store_true',
                            help="""run only if termios window size is unset
                                    (rows 0, cols 0);
                                    exit with success if already set""")
    if _has_tcsetwinsize:
        parser.add_argument('--setwinsize', action='store_true', default=None,
                            help="""set termios window size
                                    (default: true, unless -u or -c given)""")
    parser.add_argument('--fd', type=int,
                        help="""terminal file descriptor (default: stdin)""")

    def log_level(arg: str) -> int:
        level = logging.getLevelName(arg.upper())
        if not isinstance(level, int):
            raise ValueError
        return level

    parser.add_argument('--log-level', metavar='NAME', type=log_level,
                        default=logging.INFO,
                        help="""log level name, e.g. debug, info, warning""")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)
    _logger.debug(f"{_has_tcgetwinsize=}, {_has_tcsetwinsize=}")
    if not _has_tcsetwinsize and args.shell is None:
        if os.environ['SHELL'].endswith('csh'):
            args.shell = ShellType.C
        else:
            args.shell = ShellType.BOURNE
        _logger.debug(f"auto-detected {args.shell=}")
    if _has_tcsetwinsize and args.setwinsize is None:
        args.setwinsize = args.shell is None
        _logger.debug(f"auto-detected {args.setwinsize=}")
    if args.fd is None:
        _logger.debug(f"using stdin for terminal")
        args.fd = sys.stdin.fileno()
    if _has_tcgetwinsize and args.only_if_unset:
        size = termios.tcgetwinsize(args.fd)
        if size != (0, 0):
            _logger.debug("terminal size already set, nothing to do")
            return
    size = get_terminal_size(args.fd)
    _logger.debug(f"detected terminal {size=}")
    if _has_tcsetwinsize and args.setwinsize:
        _logger.debug(f"setting window size via termios")
        termios.tcsetwinsize(args.fd, size)
    if args.shell == ShellType.BOURNE:
        print(f"COLUMNS={size[1]};")
        print(f"LINES={size[0]};")
        print(f"export COLUMNS LINES;")
    elif args.shell == ShellType.C:
        print(f"set noglob;")
        print(f"setenv COLUMNS {shlex.quote(str(size[1]))};")
        print(f"setenv LINES {shlex.quote(str(size[0]))};")
        print(f"unset noglob;")
    elif args.shell is not None:
        _logger.fatal(f"don't know commands to emit for {args.shell!r}")


__all__ = ['TTYAttributes', 'tcattr_saved', 'RAW_PATCH', 'get_terminal_size']

if __name__ == '__main__':
    sys.exit(main())
