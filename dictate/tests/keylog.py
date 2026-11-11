#!/usr/bin/env python3
"""Shows EXACTLY what bytes a terminal receives. Run this in the terminal you dictate into, then trigger typing
(`dictate typetest`, then click back into this terminal within 4 s). Ctrl-D quits.
Any \\x01 \\x05 \\x02 \\x06 (Ctrl+A/E/B/F) or ESC sequences in the output mean modifiers were held or keys were mangled."""
import os, sys, termios, time, tty
fd = sys.stdin.fileno(); old = termios.tcgetattr(fd); tty.setraw(fd)
print("keylog ready: waiting for input (Ctrl-D to quit)\r"); t0 = None; n = 0
try:
    while True:
        b = os.read(fd, 4096)
        if b == b"\x04": break
        t0 = t0 or time.time(); n += len(b)
        print(f"+{(time.time()-t0)*1000:7.1f} ms  {len(b):3d} bytes  {b!r}\r")
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old); print(f"\ntotal bytes: {n}")
