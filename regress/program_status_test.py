#!/usr/bin/env python3
"""Exercise OSC 7501 forwarding through pane and client PTYs, without a
terminal emulator. Groups map to the forwarding contract: probe state
machine, reply parsing, ID rewriting, lifecycle cleanup, loss recovery
and the no-replay rule. Source-grounded assertions only."""
import base64
import errno
import fcntl
import json
import os
import pathlib
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty

OSC = b'\033]7501;'
ST = b'\033\\'
BEL = b'\007'
RECORD = re.compile(rb'\033\]7501;([^\007\033]*)(?:\007|\033\\)')
PROBE = OSC + b'?' + ST
CLEAR_NS = OSC + b'state=clear:id=tmux' + ST
ID_PREFIX = b'id=tmux/p'


def pane_worker(fifo, replies):
    tty.setraw(0)
    fd = os.open(fifo, os.O_RDWR)
    pending = b''
    with open(replies, 'ab', buffering=0) as out:
        while True:
            for ready in select.select([fd, 0], [], [])[0]:
                if ready == 0:
                    data = os.read(0, 65536)
                    if not data:
                        return
                    out.write(data)
                else:
                    pending += os.read(fd, 65536)
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        data, marker = json.loads(line)
                        os.write(1, base64.b64decode(data))
                        if marker:
                            os.write(1, b'\033]2;'
                                + marker.encode() + b'\007')


class Terminal:
    def __init__(self, test, session='a'):
        self.test = test
        self.master, slave = pty.openpty()
        # Hold a slave descriptor open: on macOS the final slave close can
        # discard tail output after the client exits. A real parent shell
        # keeps the tty open, so this matches real use.
        self.held = os.dup(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
            struct.pack('HHHH', 24, 80, 0, 0))
        def controlling_tty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        self.proc = subprocess.Popen(test.args + ['attach', '-t', session],
            stdin=slave, stdout=slave, stderr=slave, env=test.env,
            preexec_fn=controlling_tty)
        os.close(slave)
        self.raw = b''
        test.terminals.append(self)
        test.until(lambda: len(test.cmd('list-clients', '-F',
            '#{client_name}').splitlines())
            == sum(t.proc.poll() is None for t in test.terminals),
            'client attach')
        # The name is stable while attached: capture it once, so later
        # queries stay correct after other clients detach.
        self.name = test.cmd('list-clients', '-F',
            '#{client_name}').splitlines()[-1]
        self.pump(.1)

    def pump(self, duration=.05):
        if self.master is None:
            return
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if not select.select([self.master], [], [],
                max(0, end - time.monotonic()))[0]:
                break
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.raw += data

    def send(self, data):
        os.write(self.master, data)

    def frames(self):
        self.pump()
        return [m[1] for m in RECORD.finditer(self.raw)]

    def wait_frame(self, body):
        self.test.until(lambda: body in self.frames(),
            'outer frame ' + repr(body))

    def wait_bytes(self, data):
        def received():
            self.pump(.02)
            return data in self.raw
        self.test.until(received, 'terminal bytes ' + repr(data))

    def clear_capture(self):
        self.pump(.1)
        self.raw = b''

    def support(self):
        self.wait_frame(b'?')
        self.send(OSC + b'?' + ST)
        self.wait_frame(b'state=clear:id=tmux')

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        end = time.monotonic() + 5
        while self.proc.poll() is None and time.monotonic() < end:
            self.pump(.05)
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        if self.master is not None:
            # Drain the tail before the final slave close takes effect.
            self.pump(.1)
            os.close(self.master)
            self.master = None
        if self.held is not None:
            os.close(self.held)
            self.held = None


class Test:
    def __init__(self, binary):
        self.directory = tempfile.TemporaryDirectory(
            prefix='tmux-7501.', dir='/tmp')
        self.path = pathlib.Path(self.directory.name)
        self.args = [binary, '-S', str(self.path/'socket'), '-f', '/dev/null']
        self.env = dict(os.environ, TERM='xterm-256color',
            TMUX_TMPDIR=str(self.path))
        self.env.pop('TMUX', None)
        self.terminals = []
        self.number = 0
        self.server = subprocess.Popen(self.args + ['-D'], env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=open(self.path/'server.log', 'wb'))
        try:
            self.until(lambda: (self.path/'socket').exists(),
                'server socket')
            self.new_pane('a', initial=True)
            self.cmd('set', '-g', 'status', 'off')
            self.cmd('set', '-g', 'allow-rename', 'on')
            self.cmd('set', '-s', 'escape-time', '100')
        except BaseException:
            self.close()
            raise

    def cmd(self, *args):
        result = subprocess.run(self.args + list(args), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        assert result.returncode == 0, (args, result.stderr.decode())
        return result.stdout.decode().strip()

    def until(self, predicate, label, timeout=8):
        # Drain client PTYs while waiting for detach or discard output, so
        # a full client buffer cannot stall the server and the close tail
        # is captured (macOS drained-capture fix).
        draining = any(word in label for word in
            ('detach', 'disconnect', 'exit', 'discard'))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            if draining:
                for terminal in self.terminals:
                    terminal.pump(.01)
            time.sleep(.02)
        raise AssertionError('timed out: ' + label)

    def new_pane(self, session='a', initial=False, window=False):
        self.number += 1
        fifo = self.path/('fifo' + str(self.number))
        replies = self.path/('replies' + str(self.number))
        os.mkfifo(fifo)
        command = shlex.join([sys.executable, '-B',
            os.path.abspath(__file__), '--pane', str(fifo), str(replies)])
        # Qualify the target as a session: a bare name is first tried
        # as a window-name prefix in the current session, and these
        # panes are automatically named after the spawn shell ("bash"
        # on Linux), so a bare "b" would match session b's window 0.
        if initial:
            args = ['new-session', '-d', '-s', session]
        elif window:
            args = ['new-window', '-d', '-t', session + ':']
        else:
            args = ['split-window', '-d', '-t', session + ':']
        pane = self.cmd(*args, '-P', '-F', '#{pane_id}', command)
        self.until(replies.exists, 'pane worker')
        return (pane, fifo, replies)

    def write(self, data, marker, pane):
        payload = (json.dumps([base64.b64encode(data).decode(), marker])
            + '\n').encode()
        end = time.monotonic() + 10
        while True:
            try:
                fd = os.open(pane[1], os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as error:
                # The pane worker may still be starting (respawn).
                if error.errno != errno.ENXIO or time.monotonic() > end:
                    raise
                time.sleep(.02)
        try:
            end = time.monotonic() + 10
            while payload:
                assert time.monotonic() < end, 'timed out writing pane fixture'
                if select.select([], [fd], [], .05)[1]:
                    payload = payload[os.write(fd, payload):]
        finally:
            os.close(fd)

    def emit(self, data, pane=None):
        if pane is None:
            pane = self.first
        self.number += 1
        marker = 'ready' + str(self.number)
        self.write(data, marker, pane)
        self.until(lambda: self.cmd('display', '-p', '-t', pane[0],
            '#{pane_title}') == marker, marker)

    def partial(self, data):
        self.write(data, '', self.first)
        time.sleep(.05)

    def report(self, body, pane=None, end=ST):
        self.emit(OSC + body + end, pane)

    def pane_bytes(self, pane):
        return pane[2].read_bytes() if pane[2].exists() else b''

    def pane_drained(self, pane, expected):
        self.until(lambda: self.pane_bytes(pane) == expected,
            'pane bytes ' + repr(expected))

    @property
    def first(self):
        return ('%0', self.path/'fifo1', self.path/'replies1')

    def close(self):
        try:
            if self.server.poll() is None:
                self.cmd('kill-server')
            end = time.monotonic() + 10
            while self.server.poll() is None and time.monotonic() < end:
                for terminal in self.terminals:
                    terminal.pump(.01)
            for terminal in self.terminals:
                terminal.close()
            if self.server.poll() is None:
                self.server.terminate()
            self.server.wait(timeout=5)
        finally:
            self.directory.cleanup()


def probe_off(test):
    # Default off: no probe, no forwarded output, no clears on switch or
    # detach, even with a live report producer.
    terminal = Terminal(test)
    foreign = test.new_pane('b', initial=True)
    test.report(b'state=done')
    terminal.pump(.3)
    assert terminal.frames() == [], 'default-off terminal traffic'
    name = terminal.name
    test.cmd('switch-client', '-c', name, '-t', 'b')
    test.report(b'state=error', foreign)
    terminal.pump(.3)
    assert terminal.frames() == [], 'session switch emitted OSC7501 while off'
    test.cmd('switch-client', '-c', name, '-t', 'a')
    test.cmd('detach-client', '-t', name)
    test.until(lambda: terminal.proc.poll() is not None, 'detach')
    terminal.pump(.1)
    assert terminal.frames() == [], 'detach emitted OSC7501 while off'
    assert b'\033]7501;' not in terminal.raw, 'any OSC7501 while off'


def probe_query(test):
    # Enabling emits exactly the support probe, once.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_bytes(PROBE)
    assert terminal.frames() == [b'?'], terminal.frames()
    terminal.clear_capture()
    terminal.pump(.5)
    assert terminal.frames() == [], 'probe repeated'


def probe_timeout(test):
    # No reply inside the query window: unsupported, reports dropped.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    test.report(b'state=done')
    terminal.pump(5.4)
    assert terminal.frames() == [b'?'], 'report forwarded without support'
    test.report(b'state=error')
    terminal.pump(.2)
    assert terminal.frames() == [b'?'], 'report forwarded after timeout'


def probe_reply(test):
    # A valid reply enables forwarding; the confirmation clear precedes
    # later reports. An unsupported second client is not served; a
    # malformed pending reply and a stray valid reply after confirmation
    # are consumed but never applied.
    foreign = test.new_pane('b', initial=True)
    terminal = Terminal(test)
    other = Terminal(test, session='b')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    assert terminal.frames()[-1] == b'state=clear:id=tmux'
    test.report(b'state=working:app=build')
    terminal.wait_frame(b'id=tmux/p0:state=working:app=build')
    other.wait_frame(b'?')
    test.report(b'state=error', foreign)
    other.pump(.3)
    assert other.frames() == [b'?'], 'unsupported client received a report'
    # While still pending, a matched reply whose first pair is not "?"
    # is consumed but not applied: no confirmation clear, no report.
    # The whole capture stays as the oracle, so nothing is hidden.
    other.send(OSC + b'states=done' + ST)
    test.report(b'state=done', foreign)
    other.pump(.3)
    assert other.frames() == [b'?'], 'malformed reply activated forwarding'
    # BEL-terminated confirmation: the clear and reports still flow.
    other.send(OSC + b'?' + BEL)
    other.wait_frame(b'state=clear:id=tmux')
    # A stray valid reply after confirmation is consumed but not applied:
    # it must not arm a reset, or a spurious namespace clear would
    # precede the next report. Fresh capture spans the stray reply and
    # the report, so a spurious clear cannot be cleared away.
    other.clear_capture()
    other.send(OSC + b'?' + ST)
    test.report(b'state=blocked', foreign)
    other.wait_frame(ID_PREFIX + foreign[0][1:].encode()
        + b':state=blocked')
    assert other.frames() == [ID_PREFIX + foreign[0][1:].encode()
        + b':state=blocked'], 'stray reply reset forwarding'


def probe_fragments(test):
    # A reply split across client-PTY reads, within the escape time, is
    # reassembled and applied; no reply bytes reach the pane.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.send(b'x')
    for chunk in (b'\033', b']7501;?', b':unknown=1', ST):
        terminal.send(chunk)
        terminal.pump(.05)
    terminal.wait_frame(b'state=clear:id=tmux')
    terminal.send(b'y')
    test.pane_drained(test.first, b'xy')
    # Escape-prefixed reply: the Escape reaches the pane as a key, the
    # reply is still consumed and applied.
    test.cmd('set', '-s', 'forward-program-status', 'off')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    test.first[2].write_bytes(b'')
    terminal.send(b'\033' + OSC + b'?' + ST + b'z')
    terminal.wait_frame(b'state=clear:id=tmux')
    test.pane_drained(test.first, b'\033z')


def probe_oversize(test):
    # An overbound reply enters the discard window; its residue is
    # consumed, never delivered to a pane, and a fresh reply is accepted.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.send(OSC + b'?:states=' + b'x'*4200)
    terminal.pump(.1)
    terminal.send(b'residue' + ST + b'q')
    test.pane_drained(test.first, b'q')
    terminal.send(OSC + b'?' + ST + b'w')
    terminal.wait_frame(b'state=clear:id=tmux')
    test.pane_drained(test.first, b'qw')
    # The discard window also expires on its own. Re-arm it with an
    # exactly 4096-byte unterminated sequence: the parser consumes it
    # to the bound with no residue and arms the window (a matched reply
    # would have cleared it). Wait past the window with nothing sent in
    # between - no introducer or terminator may end it early - then
    # ordinary residue without a fresh introducer reaches the pane
    # again instead of being consumed forever.
    terminal.send(OSC + b'x'*(4096 - 7))
    terminal.pump(.1)
    test.pane_drained(test.first, b'qw')
    terminal.pump(5.4)
    terminal.send(b'residue' + ST + b'e')
    test.pane_drained(test.first, b'qw' + b'residue' + ST + b'e')


def probe_late(test):
    # A reply after the probe timed out is consumed but not applied.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.pump(5.4)
    terminal.clear_capture()
    terminal.send(OSC + b'?' + ST)
    test.report(b'state=done')
    terminal.pump(.2)
    assert terminal.frames() == [], 'late reply activated forwarding'
    # A fresh probe on re-enable restores forwarding.
    test.cmd('set', '-s', 'forward-program-status', 'off')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=done')
    terminal.wait_frame(b'id=tmux/p0:state=done')


def probe_notunnel(test):
    # Matched replies are always consumed, never delivered to any pane,
    # with or without a pending probe.
    terminal = Terminal(test)
    split = test.new_pane()
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.send(OSC + b'?' + ST + b'ordinary')
    terminal.wait_frame(b'state=clear:id=tmux')
    test.pane_drained(test.first, b'ordinary')
    assert test.pane_bytes(split) == b'', 'reply reached another pane'
    # Option off: still consumed, never tunneled.
    test.cmd('set', '-s', 'forward-program-status', 'off')
    test.first[2].write_bytes(b'')
    terminal.send(OSC + b'?:states=done' + ST + b'tail')
    test.pane_drained(test.first, b'tail')


def forward_basic(test):
    # Root and child identity rewriting; non-ID fields verbatim, omitted
    # app stays omitted, BEL frames keep the BEL identity.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    body = (b'state=working:app=build:msg=SGVsbG8=:title=VGVzdA=='
        b':kind=auth:progress=42:unknown=yes')
    expected = (ID_PREFIX + b'0:' + body)
    test.report(body)
    terminal.wait_frame(expected)
    terminal.clear_capture()
    test.report(b'state=done', end=BEL)
    terminal.wait_bytes(OSC + ID_PREFIX + b'0:state=done' + BEL)
    test.report(b'state=done:id=build/test')
    terminal.wait_frame(ID_PREFIX + b'0/build/test:state=done')
    # A second pane keeps an independent root.
    split = test.new_pane()
    test.report(b'state=blocked', split)
    terminal.wait_frame(ID_PREFIX + b'1:state=blocked')
    # Whitespace inside non-ID fields is preserved verbatim.
    test.report(b' state = idle : app = spaced ')
    terminal.wait_frame(ID_PREFIX + b'0: state = idle : app = spaced ')


def forward_idrules(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    # Valid duplicates: last wins after validating all.
    test.report(b'state=done:id=a/b:id=c/d')
    terminal.wait_frame(ID_PREFIX + b'0/c/d:state=done')
    terminal.clear_capture()
    # Bare, empty and invalid values drop the whole report.
    for body in (b'state=done:id', b'state=done:id=', b'state=done:id=bad!',
        b'state=done:id=has space', b'state=done:id=/lead',
        b'state=done:id=trail/', b'state=done:id=a//b'):
        test.report(body)
        assert terminal.frames() == [], ('invalid id forwarded', body)
    # Pre-translation limits.
    test.report(b'state=done:id=' + b'a'*33)
    assert terminal.frames() == [], '33-byte segment accepted'
    test.report(b'state=done:id=' + b'/'.join([b'a']*9))
    assert terminal.frames() == [], 'nine segments accepted'
    test.report(b'state=done:id=' + b'a'*129)
    assert terminal.frames() == [], '129-byte id accepted'
    # Post-translation limits: passes before, exceeds after rewriting.
    long_id = b'/'.join([b'a'*30]*4)
    test.report(b'state=done:id=' + long_id)
    assert terminal.frames() == [], 'overbound translated id accepted'
    test.report(b'state=done:id=' + b'/'.join([b'a']*8))
    assert terminal.frames() == [], 'ten-segment translated id accepted'
    # A regenerated frame of exactly 4096 bytes passes; 4097 drops whole.
    for end in (BEL, ST):
        # Rewritten body = "id=tmux/p0:" + original body (11 bytes added);
        # frame = 7 + rewritten + terminator.
        base = 4096 - 7 - 11 - len(end)
        body = b'state=done:pad=' + b'x'*(base - len(b'state=done:pad='))
        test.report(body, end=end)
        terminal.wait_frame(ID_PREFIX + b'0:' + body)
        terminal.clear_capture()
        body = b'state=done:pad=' + b'x'*(base + 1
            - len(b'state=done:pad='))
        test.report(body, end=end)
        assert terminal.frames() == [], '4097-byte frame accepted'
        terminal.clear_capture()


def forward_clear(test):
    # Clears translate by the same id rule as reports; two panes with
    # the same child id stay independent.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=clear')
    terminal.wait_frame(ID_PREFIX + b'0:state=clear')
    test.report(b'state=clear:id=build/test')
    terminal.wait_frame(ID_PREFIX + b'0/build/test:state=clear')
    split = test.new_pane()
    test.report(b'state=clear:id=test')
    test.report(b'state=clear:id=test', split)
    terminal.wait_frame(ID_PREFIX + b'0/test:state=clear')
    terminal.wait_frame(ID_PREFIX + b'1/test:state=clear')


def forward_order(test):
    # Identical and distinct reports forward in arrival order, one frame
    # each: no coalescing, no suppression.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    terminal.clear_capture()
    test.report(b'state=working')
    test.report(b'state=working')
    test.report(b'state=done')
    terminal.wait_frame(ID_PREFIX + b'0:state=done')
    assert terminal.frames() == [ID_PREFIX + b'0:state=working',
        ID_PREFIX + b'0:state=working',
        ID_PREFIX + b'0:state=done'], terminal.frames()


def local_root(test):
    # Forwarding on with support confirmed: local root behaviour is
    # unchanged and independent of forwarding.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    fmt = ('#{pane_program_state}|#{pane_program_name}'
        '|#{pane_program_message}')
    message = base64.b64encode(b'Hello').decode().encode()
    body = b'state=done:app=tool:msg=' + message
    test.report(body)
    terminal.wait_frame(ID_PREFIX + b'0:' + body)
    assert test.cmd('display', '-p', '-t', '%0', fmt) == 'done|tool|Hello'
    # Named child reports are blocked locally but forward.
    test.report(b'state=blocked:id=child')
    terminal.wait_frame(ID_PREFIX + b'0/child:state=blocked')
    assert test.cmd('display', '-p', '-t', '%0', fmt) == 'done|tool|Hello'
    # Equality suppression is local only: identical reports yield outer
    # frames each time and no local change.
    test.report(body)
    test.report(body)
    terminal.wait_frame(ID_PREFIX + b'0:' + body)
    frames = [f for f in terminal.frames() if f == ID_PREFIX + b'0:' + body]
    assert len(frames) == 3, frames
    assert test.cmd('display', '-p', '-t', '%0', fmt) == 'done|tool|Hello'
    # Root clear empties local formats and forwards.
    test.report(b'state=clear')
    terminal.wait_frame(ID_PREFIX + b'0:state=clear')
    assert test.cmd('display', '-p', '-t', '%0', fmt) == '||'
    # An inner query gets the unchanged upstream reply, never forwarded.
    test.first[2].write_bytes(b'')
    test.report(b'?')
    test.pane_drained(test.first, PROBE)
    terminal.pump(.2)
    assert not any(f.endswith(b':?') for f in terminal.frames()), \
        'query forwarded'


def scope_background(test):
    # A pane in a background window of the attached session forwards.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    hidden = test.new_pane(window=True)
    test.report(b'state=done', hidden)
    terminal.wait_frame(ID_PREFIX + hidden[0][1:].encode() + b':state=done')


def scope_session(test):
    # Panes of other sessions stay excluded; a linked window's pane
    # produces exactly one copy per client.
    foreign = test.new_pane('b', initial=True)
    linked = test.new_pane('b', window=True)
    terminal = Terminal(test)
    second = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    second.support()
    test.cmd('link-window', '-s', 'b:1', '-t', 'a:')
    test.report(b'state=working', linked)
    for client in (terminal, second):
        client.wait_frame(ID_PREFIX + linked[0][1:].encode()
            + b':state=working')
        assert client.frames().count(
            ID_PREFIX + linked[0][1:].encode() + b':state=working') == 1, \
            'linked pane sent more than one copy'
        client.clear_capture()
    test.report(b'state=error', foreign)
    terminal.pump(.2)
    second.pump(.2)
    assert terminal.frames() == [] and second.frames() == [], \
        'unrelated session pane forwarded'


def scope_move(test):
    # Non-destructive membership changes: departing clients are cleared,
    # still-member scopes keep their records.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    moved = test.new_pane()
    test.report(b'state=working', moved)
    terminal.wait_frame(ID_PREFIX + b'1:state=working')
    terminal.clear_capture()
    # Break the pane out into a new window of another session: the old
    # session's client is cleared, the new session keeps the scope for its
    # own clients.
    b_pane = test.new_pane('b', initial=True)
    test.cmd('break-pane', '-a', '-s', moved[0], '-t', 'b:0')
    terminal.wait_frame(b'state=clear:id=tmux/p1')
    test.report(b'state=done', moved)
    terminal.pump(.2)
    assert terminal.frames() == [b'state=clear:id=tmux/p1'], \
        'moved pane still reported to the old client'
    other = Terminal(test, session='b')
    other.support()
    test.report(b'state=error', moved)
    other.wait_frame(ID_PREFIX + b'1:state=error')
    # Join it back: the gaining client gets no clear and no replay; a
    # fresh report flows again.
    terminal.clear_capture()
    other.clear_capture()
    test.cmd('join-pane', '-s', moved[0], '-t', 'a:0.0')
    terminal.pump(.2)
    assert terminal.frames() == [], 'join emitted a clear to the gainer'
    assert other.frames() == [b'state=clear:id=tmux/p' +
        moved[0][1:].encode()], 'join departing client not cleared'
    test.report(b'state=done', moved)
    terminal.wait_frame(ID_PREFIX + b'1:state=done')
    # Swap panes across sessions: each client is cleared only for the
    # pane it lost; the gained pane keeps its scope and reports flow to
    # its new session's clients only.
    terminal.clear_capture()
    other.clear_capture()
    test.cmd('swap-pane', '-s', 'a:0.0', '-t', 'b:0.0')
    terminal.wait_frame(b'state=clear:id=tmux/p0')
    other.wait_frame(b'state=clear:id=tmux/p2')
    test.report(b'state=done', b_pane)
    terminal.wait_frame(ID_PREFIX + b'2:state=done')
    test.report(b'state=error')
    other.wait_frame(ID_PREFIX + b'0:state=error')
    # Still-member move: the pane's old window is linked into both
    # sessions, so session b contains the old and the new window and
    # its client keeps the records; session a only loses the pane.
    linked = test.new_pane(window=True)
    test.cmd('link-window', '-s', 'a:1', '-t', 'b:')
    terminal.clear_capture()
    other.clear_capture()
    test.cmd('join-pane', '-s', linked[0], '-t', 'b:0.0')
    terminal.wait_frame(b'state=clear:id=tmux/p' +
        linked[0][1:].encode())
    other.pump(.2)
    assert other.frames() == [], 'still-member client cleared on move'
    test.report(b'state=done', linked)
    other.wait_frame(ID_PREFIX + linked[0][1:].encode() + b':state=done')
    terminal.pump(.2)
    assert ID_PREFIX + linked[0][1:].encode() + b':state=done' not in \
        terminal.raw, 'departing client still received the pane'


def scope_swap(test):
    # Cross-session swap-window is a membership change for both sides:
    # each attached client is cleared for the window its session lost,
    # a window still linked through another winlink keeps its scope, a
    # same-session swap clears nothing, and the gained windows' reports
    # flow live with no replay.
    binit = test.new_pane('b', initial=True)
    terminal = Terminal(test)
    other = Terminal(test, session='b')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    other.support()
    swapped = test.new_pane(window=True)
    gained = test.new_pane('b', window=True)
    test.report(b'state=working', swapped)
    terminal.wait_frame(ID_PREFIX + swapped[0][1:].encode()
        + b':state=working')
    test.report(b'state=error', gained)
    other.wait_frame(ID_PREFIX + gained[0][1:].encode()
        + b':state=error')
    terminal.clear_capture()
    other.clear_capture()
    # Cross-session swap: a loses swapped's window, b loses gained's.
    test.cmd('swap-window', '-s', 'a:1', '-t', 'b:1')
    terminal.wait_frame(b'state=clear:id=tmux/p'
        + swapped[0][1:].encode())
    other.wait_frame(b'state=clear:id=tmux/p'
        + gained[0][1:].encode())
    terminal.pump(.2)
    other.pump(.2)
    assert terminal.frames() == [b'state=clear:id=tmux/p'
        + swapped[0][1:].encode()], 'cross-session swap clear count'
    assert other.frames() == [b'state=clear:id=tmux/p'
        + gained[0][1:].encode()], 'cross-session swap clear count'
    # The gained windows report live: no replay of either side's
    # earlier records.
    test.report(b'state=done', gained)
    terminal.wait_frame(ID_PREFIX + gained[0][1:].encode()
        + b':state=done')
    test.report(b'state=done', swapped)
    other.wait_frame(ID_PREFIX + swapped[0][1:].encode()
        + b':state=done')
    assert terminal.frames() == [b'state=clear:id=tmux/p'
        + swapped[0][1:].encode(),
        ID_PREFIX + gained[0][1:].encode() + b':state=done'], \
        'gained window replay or extra frames'
    assert other.frames() == [b'state=clear:id=tmux/p'
        + gained[0][1:].encode(),
        ID_PREFIX + swapped[0][1:].encode() + b':state=done'], \
        'gained window replay or extra frames'
    # A window linked into a twice keeps its scope when one link is
    # swapped away: only the other side's client is cleared.
    dup = test.new_pane(window=True)
    test.cmd('link-window', '-s', 'a:2', '-t', 'a:')
    test.report(b'state=working', dup)
    terminal.wait_frame(ID_PREFIX + dup[0][1:].encode()
        + b':state=working')
    terminal.clear_capture()
    other.clear_capture()
    test.cmd('swap-window', '-s', 'a:2', '-t', 'b:0')
    other.wait_frame(b'state=clear:id=tmux/p' + binit[0][1:].encode())
    terminal.pump(.3)
    assert terminal.frames() == [], 'retained duplicate cleared on swap'
    # The retained window keeps reporting to the client that kept it.
    test.report(b'state=done', dup)
    terminal.wait_frame(ID_PREFIX + dup[0][1:].encode() + b':state=done')
    # Same-session swap is not a membership change: nothing clears.
    terminal.clear_capture()
    other.clear_capture()
    test.cmd('swap-window', '-s', 'a:0', '-t', 'a:2')
    terminal.pump(.3)
    other.pump(.3)
    assert terminal.frames() == [], 'same-session swap cleared records'
    assert other.frames() == [], 'same-session swap cleared records'
    test.report(b'state=done')
    terminal.wait_frame(ID_PREFIX + b'0:state=done')


def scope_switch(test):
    # Switch clears old-session records, destination reports flow, and
    # window selection within a session preserves records.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    other = test.new_pane('b', initial=True)
    window = test.new_pane('b', window=True)
    terminal.clear_capture()
    name = terminal.name
    test.cmd('switch-client', '-c', name, '-t', 'b')
    terminal.wait_frame(b'state=clear:id=tmux')
    test.report(b'state=done', other)
    terminal.wait_frame(ID_PREFIX + other[0][1:].encode() + b':state=done')
    terminal.clear_capture()
    test.cmd('select-window', '-t', 'b:1')
    test.report(b'state=blocked', window)
    terminal.wait_frame(ID_PREFIX + window[0][1:].encode()
        + b':state=blocked')
    assert not any(f == b'state=clear:id=tmux' for f in terminal.frames()), \
        'window selection cleared records'
    # Same-session switch-client (window navigation) is not a session
    # change either: no clear, and reports keep flowing.
    terminal.clear_capture()
    test.cmd('switch-client', '-c', name, '-t', 'b:0')
    terminal.pump(.2)
    assert terminal.frames() == [], 'same-session switch cleared records'
    test.report(b'state=done', other)
    terminal.wait_frame(ID_PREFIX + other[0][1:].encode() + b':state=done')
    # A never-supported client gets no unsolicited output on switch or
    # detach, beyond the probe it was sent.
    naive = Terminal(test)
    naive.wait_frame(b'?')
    naive.clear_capture()
    nname = naive.name
    test.cmd('switch-client', '-c', nname, '-t', 'a')
    test.cmd('detach-client', '-t', nname)
    test.until(lambda: naive.proc.poll() is not None, 'detach')
    naive.pump(.1)
    assert naive.frames() == [], 'never-supported client emitted output'


def lifecycle_destroy(test):
    # Pane destruction clears its subtree for every eligible client of
    # the pane's session. Two confirmed same-session clients must each
    # receive exactly one copy; this pins the pane-destruction clear's
    # client loop only (pane-moved and window-departing have their own
    # loops, pinned by their own groups, not proven here).
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    other = Terminal(test)
    other.support()
    split = test.new_pane()
    test.report(b'state=working', split)
    terminal.wait_frame(ID_PREFIX + b'1:state=working')
    other.wait_frame(ID_PREFIX + b'1:state=working')
    terminal.clear_capture()
    other.clear_capture()
    test.cmd('kill-pane', '-t', split[0])
    clear = b'state=clear:id=tmux/p1'
    terminal.wait_frame(clear)
    other.wait_frame(clear)
    terminal.pump(.2)
    other.pump(.2)
    assert terminal.frames() == [clear], \
        'destroy clear not broadcast to every session client'
    assert other.frames() == [clear], \
        'destroy clear not broadcast to every session client'


def lifecycle_respawn(test):
    # Respawn keeps the same pane id and clears its subtree without
    # destroying the pane.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    terminal.clear_capture()
    command = shlex.join([sys.executable, '-B', os.path.abspath(__file__),
        '--pane', str(test.path/'fifo1'), str(test.path/'replies1')])
    test.cmd('respawn-pane', '-k', '-t', '%0', command)
    terminal.wait_frame(b'state=clear:id=tmux/p0')
    assert test.cmd('display', '-p', '-t', '%0',
        '#{pane_id} #{pane_dead}') == '%0 0', 'pane destroyed by respawn'
    # The retained pane reports again under the same identity.
    test.report(b'state=done')
    terminal.wait_frame(ID_PREFIX + b'0:state=done')


def lifecycle_unlink(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    # A window linked into two sessions: unlinking one side clears only
    # that side's clients; the still-linked side keeps its records.
    test.new_pane('b', initial=True)
    other = Terminal(test, session='b')
    other.support()
    window = test.new_pane(window=True)
    test.cmd('link-window', '-s', 'a:1', '-t', 'b:')
    test.report(b'state=done', window)
    terminal.wait_frame(ID_PREFIX + window[0][1:].encode() + b':state=done')
    other.wait_frame(ID_PREFIX + window[0][1:].encode() + b':state=done')
    terminal.clear_capture()
    other.clear_capture()
    test.cmd('unlink-window', '-t', 'a:1')
    terminal.wait_frame(b'state=clear:id=tmux/p' + window[0][1:].encode())
    other.pump(.2)
    assert other.frames() == [], 'still-linked side cleared on unlink'
    # A window linked twice into ONE session: removing one link keeps the
    # scope, because the window is still linked through the other.
    test.cmd('link-window', '-s', 'a:0', '-t', 'a:')
    terminal.clear_capture()
    test.cmd('unlink-window', '-t', 'a:1')
    terminal.pump(.2)
    assert terminal.frames() == [], 'same-session duplicate unlinked clear'
    # Last-reference close: kill-window frees the window after the
    # unlink event; the clear must arrive from the pre-removal path.
    # Keep another window so the session and client survive the kill.
    # Each real removal below fires the departing clear at entry, then
    # the last-reference destroy fires the pane clear again; only the
    # session_has recheck suppresses the duplicate, so exactly one
    # frame may arrive in each fresh capture.
    test.new_pane(window=True)
    terminal.clear_capture()
    test.cmd('kill-window', '-t', 'a:0')
    terminal.wait_frame(b'state=clear:id=tmux/p0')
    terminal.pump(.2)
    assert terminal.frames() == [b'state=clear:id=tmux/p0'], \
        'kill-window emitted more than one clear'
    # new-window -k over an occupied index (spawn.c SPAWN_KILL) is a
    # real removal: the killed window's clients get its pane clear.
    killed = test.new_pane(window=True)
    killedwin = test.cmd('display', '-p', '-t', killed[0],
        '#{window_id}')
    terminal.clear_capture()
    test.cmd('new-window', '-k', '-d', '-t', killedwin)
    killed_clear = b'state=clear:id=tmux/p' + killed[0][1:].encode()
    terminal.wait_frame(killed_clear)
    terminal.pump(.2)
    assert terminal.frames() == [killed_clear], \
        'new-window -k emitted more than one clear'
    # move-window -k onto an occupied index (server-fn killflag) is a
    # real removal too: the destination window's clients are cleared.
    spare = test.new_pane(window=True)
    victim = test.new_pane(window=True)
    victimwin = test.cmd('display', '-p', '-t', victim[0],
        '#{window_id}')
    sparewin = test.cmd('display', '-p', '-t', spare[0],
        '#{window_id}')
    terminal.clear_capture()
    test.cmd('move-window', '-k', '-s', sparewin, '-t', victimwin)
    victim_clear = b'state=clear:id=tmux/p' + victim[0][1:].encode()
    terminal.wait_frame(victim_clear)
    terminal.pump(.2)
    assert terminal.frames() == [victim_clear], \
        'move-window -k emitted more than one clear'


def lifecycle_detach(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    terminal.clear_capture()
    test.cmd('detach-client')
    test.until(lambda: terminal.proc.poll() is not None, 'detach')
    terminal.pump(.1)
    assert b'state=clear:id=tmux' in terminal.frames(), 'detach clear lost'
    clear = terminal.raw.index(CLEAR_NS)
    assert clear < terminal.raw.index(b'\033[?1049l'), \
        'clear followed terminal restoration'
    assert len([f for f in terminal.frames()
        if f == b'state=clear:id=tmux']) == 1, 'stale status after detach'
    # Reattach: re-probe; pending-window reports drop until re-confirmed.
    test.report(b'state=done')
    replacement = Terminal(test)
    replacement.wait_frame(b'?')
    test.report(b'state=error')
    replacement.pump(.2)
    assert replacement.frames() == [b'?'], 'report forwarded while pending'
    replacement.support()
    test.report(b'state=done')
    replacement.wait_frame(ID_PREFIX + b'0:state=done')


def lifecycle_suspend(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    terminal.clear_capture()
    test.cmd('suspend-client', '-t', terminal.name)
    terminal.wait_frame(b'state=clear:id=tmux')
    assert terminal.raw.index(CLEAR_NS) < terminal.raw.index(b'\033[?1049l')
    terminal.clear_capture()
    # A live report during the suspension window is dropped, not
    # queued: nothing reaches the suspended client, and nothing is
    # replayed after the resume either.
    test.report(b'state=blocked')
    terminal.pump(.2)
    assert terminal.frames() == [], 'report forwarded while suspended'
    os.kill(terminal.proc.pid, signal.SIGCONT)
    # Resume re-probes; reports in the pending window drop.
    terminal.wait_frame(b'?')
    test.report(b'state=done')
    terminal.pump(.2)
    assert terminal.frames() == [b'?'], 'report forwarded while pending'
    # The resume probe times out on an already-owned namespace: no
    # unsolicited clear (the confirmation clear, not the timeout, is
    # the residue clear), reports stay dropped while unsupported, and
    # a later re-probe with a fresh confirmation restores forwarding.
    terminal.pump(5.4)
    test.report(b'state=blocked')
    terminal.pump(.2)
    assert terminal.frames() == [b'?'], 'timeout cleared or forwarded'
    terminal.clear_capture()
    test.cmd('set', '-s', 'forward-program-status', 'off')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.clear_capture()
    terminal.send(OSC + b'?' + ST)
    terminal.wait_frame(b'state=clear:id=tmux')
    test.report(b'state=done')
    terminal.wait_frame(ID_PREFIX + b'0:state=done')
    assert ID_PREFIX + b'0:state=blocked' not in terminal.raw, \
        'suspended report replayed'


def lifecycle_disable(test):
    # The terminal override must be in place before the client attaches,
    # so its capabilities pick up the Ms extension.
    test.cmd('set', '-as', 'terminal-overrides',
        r'xterm-256color:Ms=\033]52;%p1%s;%p2%s\007'
        r'\033]7501;state=done::id=marked::app=tool\033\134')
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    # A second pane with a forwarded record, so the lifecycle clear
    # below is meaningful: the owned namespace keeps live records
    # beyond the first pane.
    split = test.new_pane()
    test.report(b'state=working', split)
    terminal.wait_frame(ID_PREFIX + split[0][1:].encode()
        + b':state=working')
    # Queue a report and the disable in one server command, so the
    # ordered clear follows the report without unrelated flushing.
    test.cmd('set-buffer', '-w', 'abc')
    terminal.wait_frame(b'state=done:id=marked:app=tool')
    terminal.clear_capture()
    test.cmd('set-buffer', '-w', 'def', ';',
        'set', '-s', 'forward-program-status', 'off')
    terminal.wait_frame(b'state=clear:id=tmux')
    terminal.pump(.1)
    assert terminal.frames() == [
        b'state=done:id=marked:app=tool',
        b'state=clear:id=tmux'], 'disable ordering'
    assert terminal.raw.index(b'\033]52;;ZGVm') < \
        terminal.raw.index(CLEAR_NS), 'unrelated output flushed'
    # Disabled: reports stop.
    terminal.clear_capture()
    test.report(b'state=working')
    terminal.pump(.2)
    assert terminal.frames() == [], 'report forwarded while disabled'
    # Lifecycle cleanup is owned, not enabled: killing the second pane
    # while the option is OFF still clears its subtree for the owned
    # client, with no probe and no report. Fresh capture spans the kill,
    # so nothing stale can hide.
    terminal.clear_capture()
    test.cmd('kill-pane', '-t', split[0])
    terminal.wait_frame(b'state=clear:id=tmux/p' + split[0][1:].encode())
    terminal.pump(.2)
    assert terminal.frames() == [b'state=clear:id=tmux/p'
        + split[0][1:].encode()], \
        'owned-disabled lifecycle clear count, probe or report'
    # Re-enable: probe, pending window, confirmation clear, no replay.
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.clear_capture()
    test.report(b'state=error')
    terminal.pump(.2)
    assert terminal.frames() == [], 'report forwarded while pending'
    terminal.send(OSC + b'?' + ST)
    terminal.wait_frame(b'state=clear:id=tmux')
    terminal.clear_capture()
    test.report(b'state=error')
    terminal.wait_frame(ID_PREFIX + b'0:state=error')
    assert terminal.frames().count(ID_PREFIX + b'0:state=error') == 1, \
        'pending-window report replayed'


def control_client(test):
    # Control clients get no probe and no raw status output.
    terminal = Terminal(test)
    control = subprocess.Popen(
        test.args + ['-C', 'attach', '-t', 'a'], env=test.env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    try:
        time.sleep(.5)
        test.cmd('set', '-s', 'forward-program-status', 'on')
        terminal.support()
        test.report(b'state=done:app=tool')
        terminal.wait_frame(ID_PREFIX + b'0:state=done:app=tool')
        os.set_blocking(control.stdout.fileno(), False)
        output = b''
        end = time.monotonic() + 1
        while time.monotonic() < end:
            try:
                output += os.read(control.stdout.fileno(), 65536)
            except BlockingIOError:
                pass
            time.sleep(.02)
        assert b'id=tmux/' not in output, 'control client received a frame'
        assert PROBE not in output, 'control client received a probe'
    finally:
        control.terminate()
        control.wait(timeout=5)


COLOUR_LINE = b''.join(b'\033[38;5;' + str(i % 256).encode() + b'mX'
    for i in range(80)) + b'\r\n'


def force_discard(test, terminal):
    # Stop reading the client PTY and render enough changing cells to
    # force the real output discard path. The public counter is
    # cumulative, so a fresh burst is proven by a strict increase.
    before = int(test.cmd('list-clients', '-F',
        '#{client_discarded}'))
    test.partial(COLOUR_LINE*1000)
    def discarded():
        if select.select([terminal.master], [], [], .01)[0]:
            terminal.raw += os.read(terminal.master, 4096)
        return int(test.cmd('list-clients', '-F',
            '#{client_discarded}')) > before
    test.until(discarded, 'terminal output discard', timeout=30)


def loss_recovery(test):
    # Known discard arms the owed clear; on resume it precedes later
    # reports. Repeated loss retries the clear.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    # Drop the confirmation clear: only the post-discard recovery clear
    # may satisfy the assertions below.
    terminal.clear_capture()
    force_discard(test, terminal)
    # A report while blocked is dropped, not queued.
    test.report(b'state=error')
    # Drain: output resumes and the owed clear precedes the next report.
    terminal.pump(1)
    terminal.wait_frame(b'state=clear:id=tmux')
    test.report(b'state=done')
    terminal.wait_frame(ID_PREFIX + b'0:state=done')
    frames = terminal.frames()
    assert frames.index(b'state=clear:id=tmux') < \
        frames.index(ID_PREFIX + b'0:state=done'), 'clear not first'
    assert ID_PREFIX + b'0:state=error' not in terminal.raw, \
        'blocked report replayed'
    # Repeated loss retries the clear: a fresh counter delta and an empty
    # capture prove the asserted clear is caused by the second discard.
    terminal.clear_capture()
    force_discard(test, terminal)
    terminal.pump(1)
    terminal.wait_frame(b'state=clear:id=tmux')
    assert terminal.frames() == [b'state=clear:id=tmux'], \
        'repeated-loss clear not alone'
    # Re-enable while output is still blocked: the sync's TTY_BLOCK
    # guard must defer the probe instead of issuing it into the
    # discard path, where it would be lost until another off/on. The
    # capture is fresh first, so no earlier query frame satisfies the
    # probe wait. The off/on follows a fresh discard immediately, so
    # the ~100 ms block window usually still covers the command; the
    # fresh delta narrows that timing race but cannot eliminate it.
    terminal.clear_capture()
    force_discard(test, terminal)
    test.cmd('set', '-s', 'forward-program-status', 'off', ';',
        'set', '-s', 'forward-program-status', 'on')
    # Drain: output resumes, the owed disable clear precedes exactly
    # one deferred probe. The probe is waited for here, so the reply
    # is sent directly rather than support(), which would wait for
    # an already-consumed probe.
    terminal.pump(1)
    terminal.wait_frame(b'?')
    assert terminal.frames() == [b'state=clear:id=tmux', b'?'], \
        'blocked re-enable probe deferral'
    # Fresh capture spans the current reply, its confirmation clear
    # and a live report: no stale clear satisfies the wait.
    terminal.clear_capture()
    terminal.send(OSC + b'?' + ST)
    terminal.wait_frame(b'state=clear:id=tmux')
    test.report(b'state=done')
    terminal.wait_frame(ID_PREFIX + b'0:state=done')


def loss_disable(test):
    # A disable clear queued while blocked is emitted when output resumes;
    # the report produced while disabled and blocked is never replayed.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    force_discard(test, terminal)
    terminal.clear_capture()
    test.cmd('set', '-s', 'forward-program-status', 'off')
    test.report(b'state=error')
    terminal.pump(1.5)
    terminal.wait_frame(b'state=clear:id=tmux')
    assert ID_PREFIX + b'0:state=error' not in terminal.raw, \
        'lost report replayed'


def loss_unowned(test):
    # Output loss never solicits a client that owns nothing: a
    # probed-but-unconfirmed client gets no clear and no report after a
    # forced discard, and a never-enabled client gets no OSC 7501 at
    # all.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.clear_capture()
    force_discard(test, terminal)
    terminal.pump(1)
    test.report(b'state=done')
    terminal.pump(.3)
    assert terminal.frames() == [], \
        'unconfirmed client cleared or forwarded after loss'
    # Never-enabled: the option stays off, so a discard owes nothing.
    fresh = Test(sys.argv[1])
    try:
        naive = Terminal(fresh)
        force_discard(fresh, naive)
        naive.pump(1)
        fresh.report(b'state=done')
        naive.pump(.3)
        assert b'\033]7501;' not in naive.raw, \
            'never-enabled client emitted output'
    finally:
        fresh.close()


def noreplay(test):
    # Reports produced while detached, disabled or unsupported never
    # appear later; only new reports after confirmation.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.cmd('detach-client')
    test.until(lambda: terminal.proc.poll() is not None, 'detach')
    terminal.pump(.1)
    test.report(b'state=working')
    test.cmd('set', '-s', 'forward-program-status', 'off')
    test.report(b'state=error')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    test.report(b'state=blocked')
    replacement = Terminal(test)
    replacement.support()
    frames = replacement.frames()
    assert not any(b'state=working' in f or b'state=error' in f or
        b'state=blocked' in f for f in frames), \
        'stale reports replayed on attach'
    # Only a fresh report flows.
    test.report(b'state=done')
    replacement.wait_frame(ID_PREFIX + b'0:state=done')


def lifecycle_kill_session(test):
    # kill-session discharges the destroyed session's records to every
    # attached client while it is still connected: the rehomed client
    # gets exactly one clear and keeps forwarding without a re-probe;
    # the exiting client gets its clear before the terminal restore.
    keeper = test.new_pane('b', initial=True)
    # detach-on-destroy defaults to on (the client exits); switch it off
    # so the first kill rehomes the client instead.
    test.cmd('set', '-t', 'a', 'detach-on-destroy', 'off')
    test.cmd('set', '-t', 'b', 'detach-on-destroy', 'off')
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    terminal.clear_capture()
    test.cmd('kill-session', '-t', 'a')
    terminal.wait_frame(b'state=clear:id=tmux')
    terminal.pump(.1)
    assert terminal.frames() == [b'state=clear:id=tmux'], \
        'rehomed client clear count'
    # No re-probe: forwarding to the destination session continues.
    test.report(b'state=done', keeper)
    terminal.wait_frame(ID_PREFIX + keeper[0][1:].encode()
        + b':state=done')
    # Last session destroyed: the client exits, and its clear is still
    # delivered before the client restores the terminal.
    terminal.clear_capture()
    test.cmd('kill-session', '-t', 'b')
    test.until(lambda: terminal.proc.poll() is not None, 'client exit')
    terminal.pump(.1)
    assert terminal.frames() == [b'state=clear:id=tmux'], \
        'exiting client clear count'
    clear = terminal.raw.index(CLEAR_NS)
    assert clear < terminal.raw.index(b'\033[?1049l'), \
        'clear followed terminal restoration'


def lifecycle_kill_server(test):
    # kill-server reaches server_send_exit (the SIGTERM/SIGINT path):
    # the owned, supported client gets its namespace clear at teardown,
    # exactly once, after the forwarded report and before the terminal
    # restore, so no report follows the final clear. A probed but
    # never-confirmed client gets nothing beyond its probe.
    foreign = test.new_pane('b', initial=True)
    terminal = Terminal(test)
    other = Terminal(test, session='b')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    terminal.clear_capture()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    other.wait_frame(b'?')
    other.clear_capture()
    test.cmd('kill-server')
    test.until(lambda: test.server.poll() is not None, 'server exit')
    test.until(lambda: terminal.proc.poll() is not None, 'client exit')
    test.until(lambda: other.proc.poll() is not None, 'client exit')
    terminal.pump(.2)
    other.pump(.2)
    assert terminal.frames() == [ID_PREFIX + b'0:state=working',
        b'state=clear:id=tmux'], 'kill-server clear count or order'
    clear = terminal.raw.index(CLEAR_NS)
    assert clear < terminal.raw.index(b'\033[?1049l'), \
        'clear followed terminal restoration'
    assert other.frames() == [], 'never-confirmed client emitted output'
    # Never-enabled: with the option never on, kill-server emits no
    # OSC7501 at all.
    fresh = Test(sys.argv[1])
    try:
        naive = Terminal(fresh)
        fresh.cmd('kill-server')
        fresh.until(lambda: fresh.server.poll() is not None, 'server exit')
        fresh.until(lambda: naive.proc.poll() is not None, 'client exit')
        naive.pump(.2)
        assert naive.frames() == [], 'never-enabled client emitted output'
        assert b'\033]7501;' not in naive.raw, \
            'any OSC7501 while never enabled'
    finally:
        fresh.close()


def lifecycle_kill_server_pending(test):
    # kill-server while the client still has a non-empty pending output
    # buffer: server_send_exit must not write the raw clear ahead of the
    # stalled backlog (a stale report would follow the final clear), so
    # the deferral branch arms the reset and program_status_client_stop
    # makes the single bounded attempt at tty_close, after the backlog
    # flushed. The frontend client alone is paused with SIGSTOP (never
    # the server, never suspend-client): it cannot send MSG_EXITING
    # until resumed, which separates server_send_exit from tty_close.
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    terminal.clear_capture()
    # Widen the PTY before pausing, so the client reports the new size
    # and the stall below stays far below TTY_BLOCK_START: the discard
    # path must never fire.
    fcntl.ioctl(terminal.master, termios.TIOCSWINSZ,
        struct.pack('HHHH', 300, 400, 0, 0))
    test.until(lambda: test.cmd('list-clients', '-F',
        '#{client_width}') == '400', 'client resize')
    pid = terminal.proc.pid
    written = int(test.cmd('list-clients', '-F', '#{client_written}'))
    discarded = int(test.cmd('list-clients', '-F',
        '#{client_discarded}'))
    paused = False
    try:
        os.kill(pid, signal.SIGSTOP)
        paused = True
        test.until(lambda: subprocess.run(['ps', '-o', 'stat=', '-p',
            str(pid)], stdout=subprocess.PIPE).stdout.decode()
            .strip().startswith('T'), 'frontend client paused')
        # Hold the master read and fill the kernel PTY buffer with the
        # burst; its tail stays queued in tty->out.
        test.partial(COLOUR_LINE*400)
        # The fresh report is parsed (the title marker proves it) and
        # queued behind the stalled backlog.
        test.report(b'state=stale')
        settled = 0
        def queued():
            nonlocal settled
            settled = int(test.cmd('list-clients', '-F',
                '#{client_written}'))
            time.sleep(.1)
            return settled == int(test.cmd('list-clients', '-F',
                '#{client_written}'))
        test.until(queued, 'client output settled')
        assert settled > written, 'pending output not accepted'
        assert int(test.cmd('list-clients', '-F',
            '#{client_discarded}')) == discarded, 'loss path fired'
        # While the frontend stays paused, kill-server reaches
        # server_send_exit with a non-empty tty->out but no MSG_EXITING.
        test.cmd('kill-server')
        # Drain the master while the frontend is still paused, so the
        # whole backlog - the stale report included - flushes before
        # the final close. Waiting for the report frame proves the
        # backlog emptied: it is the last queued output.
        def backlog_flushed():
            if select.select([terminal.master], [], [], .01)[0]:
                terminal.raw += os.read(terminal.master, 65536)
            return any(f == ID_PREFIX + b'0:state=stale'
                for f in (m[1] for m in RECORD.finditer(terminal.raw)))
        test.until(backlog_flushed, 'pending backlog flush', timeout=15)
        os.kill(pid, signal.SIGCONT)
        paused = False
        test.until(lambda: test.server.poll() is not None, 'server exit')
        test.until(lambda: terminal.proc.poll() is not None, 'client exit')
        terminal.pump(.3)
    finally:
        if paused:
            os.kill(pid, signal.SIGCONT)
            paused = False
    # Exactly one namespace clear, after the stale report, and nothing
    # after it: the deferral kept the clear behind the backlog and the
    # stop hook emitted it last. Removing only the deferral writes the
    # clear at server_send_exit into the stalled PTY, so the stale
    # report follows it and this oracle fails.
    assert terminal.frames() == [ID_PREFIX + b'0:state=stale',
        b'state=clear:id=tmux'], 'kill-server pending clear count or order'
    clear = terminal.raw.index(CLEAR_NS)
    assert clear < terminal.raw.index(b'\033[?1049l'), \
        'clear followed terminal restoration'


def lifecycle_group(test):
    # Session-group window replacement: the follower's tree is rebuilt
    # from the leader, firing window-unlinked from a temp tree. A
    # genuinely removed window clears the follower's client; retained
    # windows (replacement winlinks) and renumbered windows do not.
    follower = test.cmd('new-session', '-d', '-t', 'a', '-P', '-F',
        '#{session_name}')
    terminal = Terminal(test, session=follower)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    terminal.clear_capture()
    # Leader adds a window: the follower is rebuilt and every retained
    # window keeps its scope, so no clear is owed.
    added = test.new_pane(window=True)
    terminal.pump(.3)
    assert terminal.frames() == [], 'retained window cleared on rebuild'
    test.report(b'state=done', added)
    terminal.wait_frame(ID_PREFIX + added[0][1:].encode() + b':state=done')
    terminal.clear_capture()
    # Leader removes that window: the follower genuinely loses it.
    test.cmd('kill-window', '-t', 'a:1')
    terminal.wait_frame(b'state=clear:id=tmux/p' +
        added[0][1:].encode())
    # Renumbering the leader replaces winlinks but keeps every window:
    # retained membership must not clear.
    again = test.new_pane(window=True)
    test.report(b'state=done', again)
    terminal.wait_frame(ID_PREFIX + again[0][1:].encode() + b':state=done')
    terminal.clear_capture()
    test.cmd('move-window', '-r', '-t', 'a')
    terminal.pump(.3)
    assert terminal.frames() == [], 'renumber cleared retained windows'
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')


def lifecycle_lock(test):
    # Server lock is a screen takeover, not a terminal handover: no
    # clear, and no probe while input is stopped. A pending probe is
    # cancelled and rearmed, so the unlock resume re-probes and reports
    # flow again only after re-confirmation; nothing is replayed.
    test.cmd('set', '-g', 'lock-command', shlex.join(['sleep', '7']))
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    # Arm a fresh pending probe, then lock before it can be answered.
    test.cmd('set', '-s', 'forward-program-status', 'off')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.clear_capture()
    test.cmd('lock-server')
    # The five-second probe window passes inside the lock; the lock
    # itself must emit nothing.
    terminal.pump(.5)
    assert terminal.frames() == [], 'lock emitted OSC7501'
    test.report(b'state=blocked')
    terminal.pump(.5)
    assert terminal.frames() == [], 'report forwarded while locked'
    # The lock command exits, the client unlocks, and the resume sync
    # discharges the owed clear armed at the lock, then re-probes: the
    # exact two frames, in that order. A fresh capture separates the
    # owed clear from the confirmation clear that must follow the
    # reply, so neither can satisfy the other's assertion.
    terminal.wait_frame(b'?')
    assert terminal.frames() == [b'state=clear:id=tmux', b'?'], \
        'unlock owed clear and probe'
    terminal.clear_capture()
    terminal.send(OSC + b'?' + ST)
    terminal.wait_frame(b'state=clear:id=tmux')
    test.report(b'state=done')
    terminal.wait_frame(ID_PREFIX + b'0:state=done')
    assert ID_PREFIX + b'0:state=blocked' not in terminal.raw, \
        'locked report replayed'


def lifecycle_locked_detach(test):
    # The lock defers the discharge of owned records: nothing is emitted
    # at the lock, and a later healthy detach, switch, kill-session or
    # kill-server still delivers exactly one namespace clear before the
    # terminal restore. Never-confirmed clients are never solicited.
    test.cmd('set', '-g', 'lock-command', shlex.join(['sleep', '2']))
    test.cmd('set', '-s', 'forward-program-status', 'on')
    # Negative: a never-confirmed client, locked and detached.
    naive = Terminal(test)
    naive.wait_frame(b'?')
    naive.clear_capture()
    nname = naive.name
    test.cmd('lock-client', '-t', nname)
    naive.pump(.2)
    assert naive.frames() == [], 'lock emitted OSC7501'
    test.cmd('detach-client', '-t', nname)
    test.until(lambda: naive.proc.poll() is not None, 'detach')
    naive.pump(.1)
    assert naive.frames() == [], \
        'never-confirmed locked client emitted output'
    assert b'\033]7501;' not in naive.raw, \
        'never-confirmed locked client OSC7501'
    # Positive: supported client with forwarded records, locked
    # detach: exactly one clear, before the terminal restore.
    terminal = Terminal(test)
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(ID_PREFIX + b'0:state=working')
    terminal.clear_capture()
    name = terminal.name
    test.cmd('lock-client', '-t', name)
    terminal.pump(.2)
    assert terminal.frames() == [], 'lock emitted OSC7501'
    test.cmd('detach-client', '-t', name)
    test.until(lambda: terminal.proc.poll() is not None, 'detach')
    terminal.pump(.1)
    assert terminal.frames() == [b'state=clear:id=tmux'], \
        'locked detach clear count'
    # The lock takeover emits its own mode leaves, so the restore anchor
    # is the final one.
    assert terminal.raw.index(CLEAR_NS) < \
        terminal.raw.rindex(b'\033[?1049l'), \
        'clear followed terminal restoration'
    # Locked switch to another session, then locked detach: the lock
    # stopped the tty, so the switch's buffered clear can never flush;
    # the obligation stays armed and the detach discharges it raw.
    test.new_pane('b', initial=True)
    third = Terminal(test)
    third.support()
    test.report(b'state=working')
    third.wait_frame(ID_PREFIX + b'0:state=working')
    third.clear_capture()
    tname = third.name
    test.cmd('lock-client', '-t', tname)
    third.pump(.2)
    assert third.frames() == [], 'lock emitted OSC7501'
    test.cmd('switch-client', '-c', tname, '-t', 'b')
    third.pump(.2)
    assert third.frames() == [], 'locked switch emitted OSC7501'
    test.cmd('detach-client', '-t', tname)
    test.until(lambda: third.proc.poll() is not None, 'detach')
    third.pump(.1)
    assert third.frames() == [b'state=clear:id=tmux'], \
        'locked switch detach clear count'
    assert third.raw.index(CLEAR_NS) < \
        third.raw.rindex(b'\033[?1049l'), \
        'clear followed terminal restoration'
    # Locked kill-session with the server surviving: the destroying hook
    # keeps the suspended client's obligation armed for its final
    # close.
    second = Terminal(test)
    second.support()
    test.report(b'state=working')
    second.wait_frame(ID_PREFIX + b'0:state=working')
    second.clear_capture()
    sname = second.name
    test.cmd('lock-client', '-t', sname)
    second.pump(.2)
    assert second.frames() == [], 'lock emitted OSC7501'
    test.cmd('kill-session', '-t', 'a')
    test.until(lambda: second.proc.poll() is not None, 'client exit')
    second.pump(.1)
    assert second.frames() == [b'state=clear:id=tmux'], \
        'locked kill-session clear count'
    # The locked-exiting client ends with its exit line rather than a
    # mode-leave restore: the clear must precede it.
    assert second.raw.index(CLEAR_NS) < \
        second.raw.index(b'[exited]'), \
        'clear followed client exit line'
    # Locked kill-server: the shutdown path tears locked clients down
    # through the final close, which discharges the armed clear; the
    # never-confirmed client still gets nothing.
    fresh = Test(sys.argv[1])
    try:
        fresh.cmd('set', '-g', 'lock-command',
            shlex.join(['sleep', '2']))
        fresh.cmd('set', '-s', 'forward-program-status', 'on')
        locked = Terminal(fresh)
        quiet = Terminal(fresh)
        locked.support()
        locked.clear_capture()
        quiet.wait_frame(b'?')
        quiet.clear_capture()
        fresh.cmd('lock-server')
        locked.pump(.2)
        quiet.pump(.2)
        assert locked.frames() == [] and quiet.frames() == [], \
            'lock emitted OSC7501'
        fresh.cmd('kill-server')
        fresh.until(lambda: fresh.server.poll() is not None,
            'server exit')
        fresh.until(lambda: locked.proc.poll() is not None,
            'client exit')
        fresh.until(lambda: quiet.proc.poll() is not None,
            'client exit')
        locked.pump(.2)
        quiet.pump(.2)
        assert locked.frames() == [b'state=clear:id=tmux'], \
            'locked kill-server clear count'
        # The locked client prints no further restore or exit line on
        # this path, so the ordering pin is the lock takeover: the
        # clear must follow it. Anchor on the last alt-screen enter:
        # ncurses 6.4+ xterm terminfo appends a title-stack sequence to
        # smcup, so the takeover redraw is not one contiguous run of
        # bytes on every platform.
        assert locked.raw.index(CLEAR_NS) > \
            locked.raw.rindex(b'\x1b[?1049h'), \
            'clear preceded the lock takeover'
        assert quiet.frames() == [], \
            'never-confirmed locked client emitted output'
    finally:
        fresh.close()


def passthrough_preserved(test):
    # Unrelated DCS passthrough is preserved: the wrapper is stripped and
    # the payload arrives intact, including a long sequence under
    # NOBLOCK. No forwarding dependency; runs on base behaviour, with
    # the emitting pane in the attached client's current window.
    for mode in ('on', 'all'):
        fresh = Test(sys.argv[1])
        try:
            fresh.cmd('set', '-g', 'allow-passthrough', mode)
            terminal = Terminal(fresh)
            payload = b'passthrough-payload-' + b'0123456789'*10
            fresh.emit(b'\033Ptmux;' + payload + ST)
            terminal.wait_bytes(payload)
            assert not RECORD.search(terminal.raw), 'payload matched OSC7501'
            # Long payload with a bounded reader pause: at 80x24 it
            # exceeds TTY_BLOCK_START, so intact delivery proves the
            # passthrough NOBLOCK behaviour is preserved.
            long_payload = (b'long-' + b'0123456789abcdef'*4096)[:65536]
            assert len(long_payload) == 65536, len(long_payload)
            fresh.emit(b'\033Ptmux;' + long_payload + ST)
            time.sleep(.3)
            terminal.wait_bytes(long_payload)
            assert terminal.raw.count(long_payload) == 1, 'payload corrupted'
        finally:
            fresh.close()


GROUPS = [
    ('probe-off', probe_off),
    ('probe-query', probe_query),
    ('probe-timeout', probe_timeout),
    ('probe-reply', probe_reply),
    ('probe-fragments', probe_fragments),
    ('probe-oversize', probe_oversize),
    ('probe-late', probe_late),
    ('probe-notunnel', probe_notunnel),
    ('forward-basic', forward_basic),
    ('forward-idrules', forward_idrules),
    ('forward-clear', forward_clear),
    ('forward-order', forward_order),
    ('local-root', local_root),
    ('scope-background', scope_background),
    ('scope-session', scope_session),
    ('scope-move', scope_move),
    ('scope-swap', scope_swap),
    ('scope-switch', scope_switch),
    ('lifecycle-destroy', lifecycle_destroy),
    ('lifecycle-respawn', lifecycle_respawn),
    ('lifecycle-unlink', lifecycle_unlink),
    ('lifecycle-detach', lifecycle_detach),
    ('lifecycle-kill-session', lifecycle_kill_session),
    ('lifecycle-kill-server', lifecycle_kill_server),
    ('lifecycle-kill-server-pending', lifecycle_kill_server_pending),
    ('lifecycle-suspend', lifecycle_suspend),
    ('lifecycle-lock', lifecycle_lock),
    ('lifecycle-locked-detach', lifecycle_locked_detach),
    ('lifecycle-group', lifecycle_group),
    ('lifecycle-disable', lifecycle_disable),
    ('control-client', control_client),
    ('loss-recovery', loss_recovery),
    ('loss-disable', loss_disable),
    ('loss-unowned', loss_unowned),
    ('noreplay', noreplay),
    ('passthrough-preserved', passthrough_preserved),
]


def run():
    binary, mode = sys.argv[1:]
    if mode != 'all' and mode not in {name for name, _ in GROUPS}:
        print('unknown group: ' + mode + ' (valid: all, '
            + ', '.join(name for name, _ in GROUPS) + ')', file=sys.stderr)
        sys.exit(1)
    for name, suite in GROUPS:
        if mode not in ('all', name):
            continue
        test = Test(binary)
        try:
            suite(test)
            print(name + ': PASS', flush=True)
        except Exception:
            for terminal in test.terminals:
                print(name + ' terminal tail: '
                    + repr(terminal.raw[-2048:]), file=sys.stderr)
            print((test.path/'server.log').read_text(errors='replace'),
                file=sys.stderr)
            raise
        finally:
            test.close()


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    if len(sys.argv) > 1 and sys.argv[1] == '--pane':
        pane_worker(*sys.argv[2:])
    else:
        run()
