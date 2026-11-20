#!/usr/bin/env python3
"""Drive the HCP OS installer on a KVM guest's serial console (virsh console).

Stdlib only, so it runs on any hypervisor with python3 and virsh: no expect, no pexpect.

The playbook boots the ISO's kernel directly with `console=tty0 console=ttyS0,...`, which is
the installer's own serial mode, so every prompt arrives here. The domain is defined with
on_reboot=destroy for this boot: when the installer finishes and reboots, the VM powers off
and the console closes. That is the success signal, once the network has been confirmed.

Prompts answered (strings from the ISO's etc/rc.hcp and usr/sbin/network_setup):
  Preserve/Clear storage volumes [pce], confirm (yN), reinstall YES/NO,
  front-end IP mode, VLAN y/n, front-end address/netmask/gateway, back-end address,
  "Is this correct?" (only after checking the summary shows our addresses).

Usage: hcp_console.py <answers.json>
answers.json keys: domain, log, wipe ("c"|"p"), allow_reinstall (bool),
                   fe_ip, fe_mask, fe_gateway, be_ip, timeout (seconds)
"""
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")
QUIET = 1.5          # seconds of silence before the screen counts as waiting for input
NUDGE_AFTER = 60     # seconds of silence before pressing Enter (accepts a default / redraws)
ARROW = r"\]:\s*\n\s*---> *$"   # network_setup prompts: 'Enter the X [default]: \n   ---> '


class ConsoleError(Exception):
    pass


class ConsoleClosed(Exception):
    pass


class Console:
    def __init__(self, domain, logpath):
        self.domain = domain
        self.log = open(logpath, "a", buffering=1)
        self.pending = ""      # output since our last send (ANSI stripped)
        self.screen = ""       # rolling tail, for error context
        self.last_rx = time.time()
        self.pid = None
        self.fd = None

    def attach(self):
        state = subprocess.run(["virsh", "domstate", self.domain],
                               capture_output=True, text=True).stdout.strip()
        if state != "running":
            started = subprocess.run(["virsh", "start", self.domain], capture_output=True, text=True)
            if started.returncode != 0:
                raise ConsoleError("virsh start %s failed: %s" % (self.domain, started.stderr.strip()))
        # Attach at once: the first prompt only comes after the kernel and initrd load.
        cmd = ["virsh", "console", "--force", self.domain]
        self.log.write("### %s (domain was %s)\n" % (" ".join(cmd), state or "unknown"))
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvp(cmd[0], cmd)
        time.sleep(2)

    def close(self):
        if self.fd is not None:
            try:
                os.write(self.fd, b"\x1d")   # Ctrl-] leaves virsh console
                time.sleep(1)
            except OSError:
                pass
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
        self.log.close()

    def read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            raise ConsoleClosed()
        if not data:
            raise ConsoleClosed()
        text = ANSI.sub("", data.decode("utf-8", "replace")).replace("\r", "")
        self.log.write(text)
        self.pending += text
        self.screen = (self.screen + text)[-6000:]
        self.last_rx = time.time()
        return True

    def send(self, text):
        self.log.write("\n>>> send: %r\n" % text)
        os.write(self.fd, (text + "\r").encode())
        self.pending = ""
        time.sleep(0.5)

    def tail(self, n=1500):
        return self.screen[-n:]


def main():
    a = json.load(open(sys.argv[1]))
    con = Console(a["domain"], a["log"])
    state = {"confirmed": False, "rejects": 0}

    def reinstall(_text):
        if a.get("allow_reinstall"):
            return "YES"
        raise ConsoleError("The OS disk already holds an HCP OS. Set hcp_replace to rebuild this VM.")

    def confirm_network(text):
        # Only say yes when the summary shows exactly what we typed.
        ok = all(re.search(r":\s+%s\s*$" % re.escape(v), text, re.M)
                 for v in (a["fe_ip"], a["fe_mask"], a["fe_gateway"], a["be_ip"]))
        if ok:
            state["confirmed"] = True
            return "y"
        state["rejects"] += 1
        if state["rejects"] > 2:
            raise ConsoleError("Network summary never matched the requested addresses:\n" + con.tail())
        return "n"

    rules = [
        (r"Type your selection and press enter \[pce\]: *$", lambda t: a.get("wipe", "c")),
        (r"Are you sure you want to clear the storage volumes \(yN\): *$", lambda t: "y"),
        (r"Type 'YES' to continue, 'NO' to exit the installation: *$", reinstall),
        (r"Enter the front-end network IP mode \([^)]*\): *$", lambda t: "IPv4"),
        (r"Do you want to provide a VLAN ID for the front-end network\? \[\w*\]: *$", lambda t: "n"),
        (r"Enter the front-end network IPv4 address \[[^\]\n]*" + ARROW, lambda t: a["fe_ip"]),
        (r"Enter the front-end network IPv4 netmask \[[^\]\n]*" + ARROW, lambda t: a["fe_mask"]),
        (r"Enter the front-end network IPv4 gateway address \[[^\]\n]*" + ARROW, lambda t: a["fe_gateway"]),
        (r"Enter the back-end network IPv4 address \[[^\]\n]*" + ARROW, lambda t: a["be_ip"]),
        (r"Is this correct\? \[n\]: *$", confirm_network),
        (r"Remove the CDROM and press ENTER: *$", lambda t: ""),
    ]
    errors = [r"Exiting installation", r"Installation Terminated", r"No network interfaces found",
              r"(?m)^Error: .*IP address"]

    deadline = time.time() + int(a.get("timeout", 3600))
    nudged = 0.0
    try:
        con.attach()
        while time.time() < deadline:
            if con.read(QUIET):
                continue                       # still printing; wait for it to go quiet
            text = con.pending
            for pat in errors:
                if re.search(pat, text):
                    raise ConsoleError("Installer reported an error:\n" + con.tail())
            for pat, answer in rules:
                if re.search(pat, text):
                    con.send(answer(text))
                    break
            else:
                if time.time() - con.last_rx > NUDGE_AFTER and time.time() - nudged > NUDGE_AFTER:
                    con.log.write("\n### idle, nudging with Enter\n")
                    con.send("")
                    nudged = time.time()
        raise ConsoleError("Timed out after %ss. Last output:\n%s" % (a.get("timeout"), con.tail()))
    except ConsoleClosed:
        # on_reboot=destroy: the installer's final reboot powers the VM off and closes the console.
        if not state["confirmed"]:
            sys.stderr.write("Console closed before the network was configured:\n%s\n" % con.tail())
            con.close()
            return 1
    except ConsoleError as e:
        sys.stderr.write("%s\n" % e)
        con.log.write("\n### FAILED: %s\n" % e)
        con.close()
        return 1
    con.log.write("\n### OS install finished; VM powered off for its first disk boot\n")
    con.close()
    print(json.dumps({"changed": True, "msg": "OS installed; network configured; VM powered off"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
