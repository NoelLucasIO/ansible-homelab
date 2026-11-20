#!/bin/bash
# hcp_disk.sh probe|inject <os-disk.qcow2> [public-key-line]
#   probe : prints INSTALLED if the disk holds an HCP OS (a partition labelled root0), else EMPTY
#   inject: appends the key to root's authorized_keys_fe on root0 (the front-end sshd's key
#           file), unless it is already there. The VM must be shut off.
# Uses qemu-nbd on a free /dev/nbdN, always disconnected on exit.
set -euo pipefail
mode=$1 disk=$2 key=${3:-}
modprobe nbd max_part=16
nbd=
for n in $(seq 0 15); do
  if [ ! -e /sys/block/nbd$n/pid ]; then nbd=/dev/nbd$n; break; fi
done
[ -n "$nbd" ] || { echo "no free /dev/nbd device" >&2; exit 1; }
mnt=
cleanup() {
  [ -n "$mnt" ] && { umount "$mnt" 2>/dev/null || true; rmdir "$mnt"; }
  qemu-nbd --disconnect "$nbd" >/dev/null 2>&1 || true
}
trap cleanup EXIT
ro=--read-only; [ "$mode" = inject ] && ro=
qemu-nbd --connect="$nbd" --format=qcow2 $ro "$disk"
udevadm settle
for i in 1 2 3 4 5; do [ -e "${nbd}p1" ] && break; sleep 1; done
root=$(blkid -o device -t LABEL=root0 "${nbd}"p* 2>/dev/null | head -1 || true)
if [ "$mode" = probe ]; then
  [ -n "$root" ] && echo INSTALLED || echo EMPTY
  exit 0
fi
[ -n "$root" ] || { echo "no root0 partition on $disk: OS not installed" >&2; exit 1; }
[ -n "$key" ] || { echo "inject needs a public key" >&2; exit 1; }
mnt=$(mktemp -d)
mount "$root" "$mnt"
f="$mnt/root/.ssh/authorized_keys_fe"
install -d -m 700 "$mnt/root/.ssh"
body=$(awk '{print $2}' <<<"$key")          # match on the key material, not the comment
if grep -qF "$body" "$f" 2>/dev/null; then echo PRESENT; else
  printf '%s\n' "$key" >> "$f"; chmod 600 "$f"; echo INJECTED
fi
