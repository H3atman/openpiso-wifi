"""Drive a RouterOS serial console via `virsh console`.

Usage: python3 -I lab/chr/rcon.py DOMAIN PASSWORD 'cmd1' 'cmd2' ...
Logs in as admin (if a login prompt appears), runs each command, prints output.
"""
import os, pty, sys, time, select

dom, password, cmds = sys.argv[1], sys.argv[2], sys.argv[3:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp("virsh", ["virsh", "-c", "qemu:///system", "console", "--force", dom])

buf = b""

def read(secs):
    global buf
    end = time.time() + secs
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                return
            buf += data

def send(s):
    os.write(fd, s.encode())

def wait_for(token, secs):
    end = time.time() + secs
    while time.time() < end:
        read(0.5)
        if token in buf[-400:]:
            return True
    return False

read(1)
send("\r")
read(2)
if b"Login:" in buf[-300:]:
    send("admin\r"); wait_for(b"assword:", 10)
    send(password + "\r"); read(4)
    if b"[Y/n]" in buf[-300:]:
        send("n\r"); read(2)
send("\r"); wait_for(b"] >", 15)
for c in cmds:
    buf = b""
    send(c + "\r")
    wait_for(b"] >", 20)
    sys.stdout.write(buf.decode(errors="replace"))
send("\x1d")
os.close(fd)
