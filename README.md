# resizewin: Detect and configure terminal size

## Usage

### From a terminal shell

```shell
eval `resizewin`
```

It works with VT100-compatible terminals, including all modern terminal emulators.

### From Python code

```python
import sys
import termios

import resizewin

fd = sys.stdin.fileno()
rows, cols = resizewin.get_terminal_size(fd)
termios.tcsetwinsize(fd, (rows, cols))
```

## How it works

In order to detect the terminal size,
`resizewin` first tells the terminal to move the cursor to as far as possible,
then asks the terminal for the current cursor's position.
It uses the fact that terminals move the cursor to the bottom right corner
when told to move the cursor to a row/column position that exceeds screen size.
