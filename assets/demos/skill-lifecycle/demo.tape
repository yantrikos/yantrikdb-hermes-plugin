# Skill-lifecycle demo for yantrikdb-hermes-plugin.
#
# Render: vhs demo.tape  (produces demo.gif + demo.mp4)
# Requires: vhs >= v0.10, python3, yantrikdb + yantrikdb-hermes-plugin installed.
#
# Tested on Linux (Ubuntu via WSL works); Windows VHS v0.11 has known
# shell-detection issues that hang the render — use WSL or render on a
# Linux/macOS box.

Output demo.gif
Output demo.mp4

Set FontSize 13
Set Width 1280
Set Height 800
Set Padding 12
Set Theme "Catppuccin Mocha"

# Hide the setup, show only the demo.
Hide
Type "cd /mnt/c/Users/sync/codes/yantrikdb-hermes-plugin && clear"
Enter
Sleep 200ms
Show

Type "python3 assets/demos/skill-lifecycle/demo.py"
Sleep 600ms
Enter

# demo.py walks 5 lifecycle steps with built-in pause() calls — ~25s total.
Sleep 30s
