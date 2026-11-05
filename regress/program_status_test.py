#!/usr/bin/env python3
"""Exercise OSC 7501 through pane and client PTYs, without a terminal emulator."""
import base64
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
RECORD = re.compile(rb'\033\]7501;([^\007\033]*)(?:\007|\033\\)')


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
                            os.write(1, b'\033]2;' + marker.encode() + b'\007')


class Terminal:
    def __init__(self, test, session='a'):
        self.test = test
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        def controlling_tty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        self.proc = subprocess.Popen(test.args + ['attach', '-t', session],
            stdin=slave, stdout=slave, stderr=slave, env=test.env,
            preexec_fn=controlling_tty)
        os.close(slave)
        self.raw = b''
        test.terminals.append(self)
        test.until(lambda: len(test.cmd('list-clients', '-F', '#{client_name}').splitlines())
                   == sum(t.proc.poll() is None for t in test.terminals), 'client attach')
        self.pump(.1)

    def pump(self, duration=.05):
        if self.master is None:
            return
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if not select.select([self.master], [], [], max(0, end-time.monotonic()))[0]:
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

    def frames(self, start=0):
        self.pump()
        return [m[1] for m in RECORD.finditer(self.raw, start)]

    def wait_frame(self, body):
        self.test.until(lambda: body in self.frames(), 'outer frame ' + repr(body))

    def wait_bytes(self, data):
        def received():
            self.pump(.02)
            return data in self.raw
        self.test.until(received, 'terminal bytes ' + repr(data))

    def clear_capture(self):
        self.pump(.1)
        self.raw = b''

    def support(self, reply=b'?', terminator=ST):
        self.wait_frame(b'?')
        self.send(OSC + reply + terminator)
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
            os.close(self.master)
            self.master = None



class Test:
    def __init__(self, binary):
        self.directory = tempfile.TemporaryDirectory(prefix='tmux-7501.', dir='/tmp')
        self.path = pathlib.Path(self.directory.name)
        self.args = [binary, '-S', str(self.path/'socket'), '-f', '/dev/null']
        self.env = dict(os.environ, TERM='xterm-256color', TMUX_TMPDIR=str(self.path))
        self.env.pop('TMUX', None)
        self.terminals = []
        self.number = 0
        self.server = subprocess.Popen(self.args + ['-D'], env=self.env,
            stdout=subprocess.DEVNULL, stderr=open(self.path/'server.log', 'wb'))
        try:
            self.until(lambda: (self.path/'socket').exists(), 'server socket')
            self.new_pane('a', initial=True)
            self.cmd('set', '-g', 'status', 'off')
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
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.02)
        raise AssertionError('timed out: ' + label)

    def new_pane(self, session='a', initial=False, window=False):
        self.number += 1
        fifo = self.path/('fifo' + str(self.number))
        replies = self.path/('replies' + str(self.number))
        os.mkfifo(fifo)
        command = shlex.join([sys.executable, '-B', os.path.abspath(__file__),
                              '--pane', str(fifo), str(replies)])
        if initial:
            args = ['new-session', '-d', '-s', session]
        elif window:
            args = ['new-window', '-d', '-t', session]
        else:
            args = ['split-window', '-d', '-t', session]
        pane = self.cmd(*args, '-P', '-F', '#{pane_id}', command)
        self.until(replies.exists, 'pane worker')
        return (pane, fifo, replies)

    def write(self, data, marker, pane):
        payload = (json.dumps([base64.b64encode(data).decode(), marker]) + '\n').encode()
        fd = os.open(pane[1], os.O_WRONLY | os.O_NONBLOCK)
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
        self.until(lambda: self.cmd('display', '-p', '-t', pane[0], '#{pane_title}')
                   == marker, marker)

    def partial(self, data, pane=None):
        self.write(data, '', pane or self.first)
        time.sleep(.05)

    def report(self, body, pane=None, end=ST):
        self.emit(OSC + body + end, pane)

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


def local(test):
    test.report(b'?')
    expected = OSC + b'?:states=idle,working,done,blocked,error:kinds=permission,question,auth' + ST
    test.until(lambda: expected in test.first[2].read_bytes(), 'detached local reply')
    test.first[2].write_bytes(b'')
    terminal = Terminal(test)
    assert terminal.frames() == [], 'default-off terminal traffic'
    test.report(b'?')
    expected = OSC + b'?:states=idle,working,done,blocked,error:kinds=permission,question,auth' + ST
    test.until(lambda: expected in test.first[2].read_bytes(), 'local feature reply')
    assert terminal.frames() == [], 'application query escaped to outer terminal'
    test.first[2].write_bytes(b'')
    test.cmd('set', '-s', 'set-clipboard', 'on')
    test.cmd('set', '-s', 'get-clipboard', 'request')
    test.emit(b'\033]52;c;?' + ST + OSC + b'?' + ST)
    terminal.wait_bytes(b'\033]52;;?')
    assert expected not in test.first[2].read_bytes(), 'local reply overtook pending request'
    terminal.send(b'\033]52;c;SGk=' + ST)
    test.until(lambda: expected in test.first[2].read_bytes(), 'queued local reply')
    assert test.first[2].read_bytes().index(b'\033]52;') < test.first[2].read_bytes().index(OSC)



def publication(test):
    terminal = Terminal(test)
    test.report(b'state=working:app=build:msg=SGVsbG8=:title=VGVzdA==:progress=42')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    terminal.wait_frame(b'state=working:id=tmux/p0:app=build:msg=SGVsbG8=:title=VGVzdA==:progress=42')
    assert terminal.frames().index(b'state=clear:id=tmux') < len(terminal.frames()) - 1
    terminal.clear_capture()
    test.report(b'state=working:app=build:msg=SGVsbG8=:title=VGVzdA==:progress=42')
    assert terminal.frames() == [], 'unchanged report resent'


def parser(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    valid = b'state=blocked:kind=auth:app=tool:msg=SGVsbG8=:title=VGVzdA==:progress=7'
    expected = b'state=blocked:id=tmux/p0:app=tool:msg=SGVsbG8=:title=VGVzdA==:kind=auth:progress=7'
    test.report(valid, end=b'\007')
    terminal.wait_frame(expected)
    terminal.clear_capture()
    invalid = [b'state=done:msg=A===', b'state=done:msg=AA==',
        b'state=done:msg=woA=', b'state=done:msg=/w==',
        b'state=done:app=' + b'a'*33, b'state=done:msg=' + b'A'*2733,
        b'state=done:title=' + b'A'*257,
        b'state=done:abcdefghijklmnopq=x', b'state=future',
        b'state=done:id=worker', b'state=done:id=bad!', b'state=done:msg=' + base64.b64encode(b'a'*2049),
        b'state=done:' + b'x'*4080]
    for body in invalid:
        test.report(body)
        assert terminal.frames() == [], ('invalid report mutated retained root', body[:80])
    for abort in (b'\030', b'\032', b'\033[0m'):
        test.emit(OSC + b'state=done' + abort)
        assert terminal.frames() == [], ('aborted frame accepted', abort)
    test.emit(OSC + b'state=do\000ne' + ST)
    assert terminal.frames() == [], 'altered C0 payload accepted'
    test.partial(OSC + b'state=done' + b'\033')
    assert terminal.frames() == [], 'unconfirmed ST accepted'
    test.emit(b'\\')
    terminal.wait_frame(b'state=done:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    test.report(b'state=working:state=error:bad:value=bad!:unknown=yes')
    terminal.wait_frame(b'state=error:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    test.report(b' state = working : app = good-app : msg = SGk : progress = 100 ')
    terminal.wait_frame(b'state=working:id=tmux/p0:app=good-app:msg=SGk=:progress=100')
    terminal.clear_capture()
    test.report(b'state=working:app=good-app:msg=SGk=:progress=100')
    assert terminal.frames() == [], 'optional padding changed normalized report'
    test.report(b'state=done:app=' + b'a'*32 + b':msg=' + base64.b64encode(b'm'*2048)
                + b':title=' + base64.b64encode(b't'*192))
    terminal.wait_frame(b'state=done:id=tmux/p0:app=' + b'a'*32 + b':msg='
                        + base64.b64encode(b'm'*2048) + b':title=' + base64.b64encode(b't'*192))
    test.report(b'state=working')
    terminal.clear_capture()
    body = b'state=error:extra=' + b'x'*(4087-len(b'state=error:extra='))
    test.report(body)
    terminal.wait_frame(b'state=error:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    body = b'state=working:extra=' + b'x'*(4088-len(b'state=working:extra='))
    test.report(body)
    assert terminal.frames() == [], '4097 byte frame accepted'
    for state in (b'idle', b'working', b'blocked', b'done', b'error'):
        test.report(b'state=' + state)
        terminal.clear_capture()
        test.emit(b'\033]133;A\007')
        frames = terminal.frames()
        assert (b'state=clear:id=tmux/p0' in frames) == (state not in (b'done', b'error')), (state, frames)
    test.report(b'state=clear')
    terminal.wait_frame(b'state=clear:id=tmux/p0')
    terminal.clear_capture()
    test.report(b'state=clear')
    assert terminal.frames() == [], 'repeated clear resent'
    test.report(b'state=working')
    terminal.clear_capture()
    test.emit(b'\033[?1049h\033[?1049l\033[!p')
    assert terminal.frames() == [], 'alternate screen or soft reset cleared root'
    test.emit(b'\033c')
    terminal.wait_frame(b'state=clear:id=tmux/p0')


def probe(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.send(OSC + b'not-a-reply' + ST)
    assert b'state=clear:id=tmux' not in terminal.frames(), 'malformed reply enabled forwarding'
    reply = OSC + b'?:states=working:states=done,blocked:kinds=auth:kinds=question' + ST
    terminal.send(b'x\033' + reply[:10])
    terminal.pump(.7)
    terminal.send(reply[10:-1])
    terminal.pump(.05)
    terminal.send(reply[-1:] + b'y')
    terminal.wait_frame(b'state=clear:id=tmux')
    test.until(lambda: b'x\033y' in test.first[2].read_bytes(), 'ordinary keys around reply')
    assert test.first[2].read_bytes() == b'x\033y', test.first[2].read_bytes()
    test.report(b'state=blocked:kind=auth')
    terminal.wait_frame(b'state=blocked:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    test.report(b'state=working')
    terminal.wait_frame(b'state=clear:id=tmux/p0')
    other = Terminal(test)
    other.support(terminator=b'\007')
    other.wait_frame(b'state=working:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    test.cmd('set', '-s', 'forward-program-status', 'off')
    terminal.wait_frame(b'state=clear:id=tmux')
    terminal.clear_capture()
    terminal.send(OSC + b'?' + ST)
    test.report(b'state=done')
    assert terminal.frames() == [], 'disabled late reply activated forwarding'
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.pump(5.3)
    terminal.clear_capture()
    terminal.send(OSC + b'?' + ST)
    assert terminal.frames() == [], 'timed-out late reply activated forwarding'
    test.cmd('set', '-s', 'forward-program-status', 'off')
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    terminal.wait_frame(b'state=done:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    terminal.send(OSC + b'?:states=' + b'x'*4100 + ST + b'z')
    test.until(lambda: test.first[2].read_bytes().endswith(b'z'), 'key after oversized reply')
    assert test.first[2].read_bytes() == b'x\033yz', 'terminal reply leaked into pane'
    terminal.send(OSC + b'?:states=')
    terminal.pump(5.3)
    terminal.send(b'q')
    test.until(lambda: test.first[2].read_bytes().endswith(b'q'), 'key after fragment timeout')
    terminal.send(b'\033')
    terminal.pump(.7)
    assert test.first[2].read_bytes() == b'x\033yzq\033', 'fragment buffering swallowed ordinary Escape'



def sync(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    hidden = test.new_pane(window=True)
    split = test.new_pane()
    foreign = test.new_pane('b', initial=True)
    test.report(b'state=done')
    test.report(b'state=blocked:kind=permission', hidden)
    test.report(b'state=error', foreign)
    test.cmd('switch-client', '-t', 'b')
    terminal.support()
    terminal.wait_frame(b'state=error:id=tmux/p3:app=tmux')
    assert not any(b'id=tmux/p0' in frame for frame in terminal.frames()), 'obsolete session snapshot'
    terminal.clear_capture()
    test.cmd('link-window', '-s', 'a:0', '-t', 'b:')
    terminal.wait_frame(b'state=done:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    test.cmd('switch-client', '-t', 'a')
    terminal.wait_frame(b'state=clear:id=tmux/p3')
    terminal.wait_frame(b'state=blocked:id=tmux/p1:app=tmux:kind=permission')
    assert not any(b'id=tmux/p0' in frame for frame in terminal.frames()), 'unchanged linked pane resent'
    test.cmd('kill-pane', '-t', hidden[0])
    terminal.wait_frame(b'state=clear:id=tmux/p1')
    test.report(b'state=working', split)
    terminal.wait_frame(b'state=working:id=tmux/p2:app=tmux')
    test.cmd('move-pane', '-s', split[0], '-t', 'b:0')
    terminal.wait_frame(b'state=clear:id=tmux/p2')
    terminal.clear_capture()
    test.cmd('detach-client')
    test.until(lambda: terminal.proc.poll() is not None, 'detach')
    terminal.pump(.1)
    assert b'state=clear:id=tmux' in terminal.frames(), 'detach clear lost'
    clear = terminal.raw.index(OSC + b'state=clear:id=tmux' + ST)
    assert clear < terminal.raw.index(b'\033[?1049l'), 'clear followed terminal restoration'
    assert len(terminal.frames()) == 1, 'stale status followed detach clear'
    replacement = Terminal(test)
    replacement.support()
    replacement.wait_frame(b'state=done:id=tmux/p0:app=tmux')
    os.close(replacement.master)
    replacement.master = None
    test.until(lambda: replacement.proc.poll() is not None, 'disconnected client exit')
    test.until(lambda: test.cmd('list-clients', '-F', 'x') == '', 'disconnected client removal')
    replacement = Terminal(test)
    replacement.support()
    replacement.wait_frame(b'state=done:id=tmux/p0:app=tmux')



def lifecycle(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=done')
    terminal.wait_frame(b'state=done:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    test.cmd('suspend-client')
    terminal.wait_frame(b'state=clear:id=tmux')
    assert terminal.raw.index(OSC + b'state=clear:id=tmux' + ST) < terminal.raw.index(b'\033[?1049l')
    terminal.clear_capture()
    os.kill(terminal.proc.pid, signal.SIGCONT)
    terminal.support()
    terminal.wait_frame(b'state=done:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    for state in (b'working', b'blocked', b'idle', b'done', b'error'):
        pane = test.new_pane()
        test.cmd('set', '-p', '-t', pane[0], 'remain-on-exit', 'on')
        test.cmd('respawn-pane', '-k', '-t', pane[0],
                 "printf '\\033]7501;state=" + state.decode() + "\\007'")
        test.until(lambda: test.cmd('display', '-p', '-t', pane[0], '#{pane_dead}') == '1',
                   'retained dead pane')
        terminal.pump(.1)
        frame = b'state=' + state + b':id=tmux/p' + pane[0][1:].encode() + b':app=tmux'
        if state in (b'done', b'error'):
            terminal.wait_frame(frame)
        else:
            # Reattach confirms replay of final retained state, independently
            # of whether an intermediate report was coalesced before PTY exit.
            test.cmd('set', '-s', 'forward-program-status', 'off')
            terminal.clear_capture()
            test.cmd('set', '-s', 'forward-program-status', 'on')
            terminal.support()
            assert frame not in terminal.frames(), (state, 'dead pane replayed transient status')
        test.cmd('kill-pane', '-t', pane[0])
    test.report(b'state=working')
    terminal.wait_frame(b'state=working:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    test.cmd('respawn-pane', '-k', '-t', '%0', 'sleep 100')
    terminal.wait_frame(b'state=clear:id=tmux/p0')


def backpressure(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.report(b'state=working')
    terminal.wait_frame(b'state=working:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    # Stop reading the client PTY and render enough changing cells to force
    # tmux's real output discard path. Check its public counter as evidence.
    line = b''.join(b'\033[38;5;' + str(i % 256).encode() + b'mX' for i in range(80)) + b'\r\n'
    test.partial(line * 1000)
    def discarded():
        if select.select([terminal.master], [], [], .01)[0]:
            terminal.raw += os.read(terminal.master, 4096)
        return int(test.cmd('list-clients', '-F', '#{client_discarded}')) > 0
    test.until(discarded, 'terminal output discard')
    test.cmd('set', '-s', 'forward-program-status', 'off', ';',
             'set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    frames = terminal.frames()
    assert frames.index(b'state=clear:id=tmux') < frames.index(b'?'), 'lost disabled cleanup'
    terminal.send(OSC + b'?' + ST)
    test.partial(OSC + b'state=error' + ST)
    test.until(lambda: b'state=error:id=tmux/p0:app=tmux' in terminal.frames(),
               'replay after output discard', timeout=30)
    frames = terminal.frames()
    assert b'state=clear:id=tmux' in frames, 'lost publication knowledge was reused'
    assert frames.index(b'state=clear:id=tmux') < frames.index(b'state=error:id=tmux/p0:app=tmux')
    terminal.clear_capture()
    latest = b'state=done:msg=TGF0ZXN0'
    test.partial(line * 1000 + OSC + latest + ST + b'\033]2;latest-ready\007')
    def latest_ready():
        if select.select([terminal.master], [], [], .01)[0]:
            terminal.raw += os.read(terminal.master, 4096)
        return test.cmd('display', '-p', '-t', '%0', '#{pane_title}') == 'latest-ready'
    test.until(latest_ready, 'changed status queued under pressure', timeout=30)
    # Disable without reading, then require cleanup after output resumes.
    clipboard = test.path/'clipboard'
    clipboard.write_text('x'*32000)
    start = time.monotonic()
    test.cmd('load-buffer', '-w', str(clipboard), ';',
             'set', '-s', 'forward-program-status', 'off')
    assert time.monotonic() - start < 2, 'disable blocked on a full terminal PTY'
    terminal.wait_frame(b'state=clear:id=tmux')
    # Preserve the disabled capture above. Check a fresh attachment snapshot
    # independently, including byte order and absence of the superseded state.
    enabled_at = len(terminal.raw)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    test.until(lambda: b'?' in terminal.frames(enabled_at), 'probe after disable')
    terminal.send(OSC + b'?' + ST)
    latest_frame = b'state=done:id=tmux/p0:app=tmux:msg=TGF0ZXN0'
    test.until(lambda: latest_frame in terminal.frames(enabled_at), 'latest snapshot after disable')
    enabled = terminal.frames(enabled_at)
    assert enabled.index(b'state=clear:id=tmux') < enabled.index(latest_frame)
    assert b'state=error:id=tmux/p0:app=tmux' not in enabled, 'obsolete status replayed'



def fragment_timer(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.wait_frame(b'?')
    terminal.send(b'\033')
    terminal.pump(.04)
    terminal.send(b']7501;?:states=')
    terminal.pump(.7)
    terminal.send(b'done' + ST + b'z')
    terminal.wait_frame(b'state=clear:id=tmux')
    test.until(lambda: test.first[2].read_bytes() == b'z', 'split prefix reply without leaked tail')
    test.first[2].write_bytes(b'')
    terminal.send(b'\033\033]7501')
    terminal.pump(.05)
    test.cmd('set', '-s', 'forward-program-status', 'off')
    terminal.send(b'z')
    test.until(lambda: test.first[2].read_bytes() == b'\033z',
               'disable discarded Escape before partial reply prefix')
    test.cmd('set', '-s', 'escape-time', '1000')
    for pending, expected in ((b'', b'z'), (b'\033', b'\033z'),
                              (b'\033\033', b'\033\033z'),
                              (OSC[:3], b'z'),
                              (b'\033' + OSC[:2], b'\033z'),
                              (b'\033\033]7501', b'\033z'),
                              (b'\033' + OSC, b'\033z')):
        test.first[2].write_bytes(b'')
        terminal.clear_capture()
        test.cmd('set', '-s', 'forward-program-status', 'on')
        terminal.support()
        terminal.send(OSC + b'x'*(4096-len(OSC)))
        terminal.pump(.05)
        if pending:
            terminal.send(pending)
            terminal.pump(.05)
        test.cmd('set', '-s', 'forward-program-status', 'off')
        terminal.send(b'z')
        test.until(lambda: test.first[2].read_bytes() == expected,
                   'discard reset with pending ' + repr(pending))


def discard_retry(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    terminal.send(OSC + b'x'*(4096-len(OSC)))
    terminal.pump(.05)
    terminal.send(OSC[:3])
    terminal.pump(.8)
    terminal.send(OSC[3:] + b'?' + ST + b'q')
    test.until(lambda: test.first[2].read_bytes() == b'q',
               'fragmented fresh introducer leaked during oversized discard')
    terminal.send(OSC + b'?:states=' + b'x'*4100)
    terminal.pump(.1)
    test.cmd('set', '-s', 'forward-program-status', 'off')
    terminal.clear_capture()
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    terminal.send(b'z')
    test.until(lambda: test.first[2].read_bytes() == b'qz', 'retry did not leak old reply fragments')
    terminal.send(OSC + b'?:states=' + b'x'*4100)
    terminal.pump(.1)
    terminal.send(OSC + b'?' + ST + b'q')
    test.until(lambda: test.first[2].read_bytes() == b'qzq', 'fresh reply during discard swallowed keys')
    test.cmd('set', '-s', 'forward-program-status', 'off')
    terminal.send(OSC + b'?:states=' + b'x'*4100)
    terminal.pump(.1)
    terminal.send(ST + b'v')
    test.until(lambda: test.first[2].read_bytes() == b'qzqv', 'disabled oversized reply leaked its tail')



def disable_flush(test):
    # Append a report to Ms so set-buffer and disable queue both frames in
    # one server command, independently of asynchronous pane parsing.
    test.cmd('set', '-as', 'terminal-overrides',
             r'xterm-256color:Ms=\033]52;%p1%s;%p2%s\007'
             r'\033]7501;state=done::id=tmux/p0::app=tmux\033\134')
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    test.cmd('set-buffer', '-w', 'abc')
    terminal.wait_bytes(b'\033]52;;YWJj')
    terminal.clear_capture()
    test.cmd('set-buffer', '-w', 'def', ';', 'set', '-s', 'forward-program-status', 'off')
    terminal.wait_frame(b'state=clear:id=tmux')
    terminal.pump(.1)
    assert terminal.frames() == [b'state=done:id=tmux/p0:app=tmux',
                                 b'state=clear:id=tmux'], 'queued report followed teardown clear'
    assert terminal.raw.index(b'\033]52;;ZGVm') < terminal.raw.index(OSC + b'state=clear:id=tmux')


def disable_clipboard(test):
    terminal = Terminal(test)
    test.cmd('set', '-s', 'forward-program-status', 'on')
    terminal.support()
    clipboard = test.path/'clipboard'
    clipboard.write_bytes(bytes(range(256))*390 + b'clipboard-end')
    expected = b'\033]52;;' + base64.b64encode(clipboard.read_bytes()) + b'\007'
    for disable in (False, True):
        terminal.clear_capture()
        args = ['load-buffer', '-w', str(clipboard)]
        if disable:
            args += [';', 'set', '-s', 'forward-program-status', 'off']
        test.cmd(*args)
        terminal.pump(1)
        assert expected in terminal.raw, ('truncated clipboard', disable, len(terminal.raw))
        if disable:
            terminal.wait_frame(b'state=clear:id=tmux')
            assert terminal.raw.index(expected) < terminal.raw.index(OSC + b'state=clear:id=tmux')


def run():
    binary, mode = sys.argv[1:]
    assert mode in ('local', 'parser', 'probe', 'sync', 'all', 'fragment-timer', 'discard-retry', 'disable-flush', 'disable-clipboard')
    suites = [('local', 'local', local), ('parser', 'parser', parser),
              ('probe', 'publication', publication),
              ('probe', 'fragment-timer', fragment_timer),
              ('probe', 'discard-retry', discard_retry),
              ('sync', 'disable-flush', disable_flush),
              ('sync', 'disable-clipboard', disable_clipboard), ('probe', 'probe', probe),
              ('sync', 'sync', sync), ('sync', 'lifecycle', lifecycle),
              ('sync', 'backpressure', backpressure)]
    for group, name, suite in suites:
        if mode not in ('all', group, name):
            continue
        test = Test(binary)
        try:
            suite(test)
            print(name + ': PASS', flush=True)
        except Exception:
            for terminal in test.terminals:
                print(name + ' terminal tail: ' + repr(terminal.raw[-2048:]), file=sys.stderr)
            print((test.path/'server.log').read_text(errors='replace'), file=sys.stderr)
            raise
        finally:
            test.close()


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    if sys.argv[1] == '--pane':
        pane_worker(*sys.argv[2:])
    else:
        run()
