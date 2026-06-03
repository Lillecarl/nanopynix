# How AI can rebuild my system to iterate on pynixd as a system daemon
## Check logs
```bash
journalctl -u pynixd -S "$(systemctl show pynixd -p ActiveEnterTimestamp --value)" # pipe or tail but prefer cleaning the logs with the filter file
```
Checks the logs of the program since it last started
## Rebuild without pynixd
```bash
time timeout 300 sudo ai-nixos-rebuild # <important>don't pipe away output!! </important>
```
## Rebuild with pynixd
```bash
time timeout 300 sudo ai-nixos-rebuild-pynixd # <important>don't pipe away output!!!</important>
```
## Collect garbage
```bash
nix-collect-garbage
```
## Filter logs
edit pynixd/filters/scheduler_focus.py

# Goal
Make sure pynixd doesn't spend time doing things it doesn't have to do

## How to accomplish
Add relevant "tracing logs"

## rebuilding pynixd
In default.nix there's an impurity which you can turn on or off to rebuild pynixd which causes a lot slower builds for some reason (so it's a good benchmark)
