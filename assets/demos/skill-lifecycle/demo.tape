# Skill lifecycle demo for yantrikdb-hermes-plugin.
#
# Render with: vhs demo.tape  (produces demo.gif and demo.mp4)
#
# The demo.py script does the actual work — this tape just paces the
# recording. Demo finishes in ~25 seconds; the trailing Sleep gives
# the final state time to read on screen.

Output demo.gif
Output demo.mp4

# Terminal size + appearance.
Set FontSize 14
Set Width 1280
Set Height 800
Set Padding 16
Set Theme "Catppuccin Mocha"
Set Shell "powershell"

# Hide the setup steps — viewers see the demo, not the env prep.
Hide
Type `$env:PYTHONIOENCODING = "utf-8"`
Enter
Type `$HERMES_PY = "C:\Users\sync\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"`
Enter
Type "cd C:\Users\sync\codes\yantrikdb-hermes-plugin"
Enter
Type "clear"
Enter
Sleep 500ms
Show

# The actual demo run.
Type "& $HERMES_PY .\assets\demos\skill-lifecycle\demo.py"
Sleep 800ms
Enter

# The script takes ~25s to walk through all five steps with the
# pause() calls baked in. Give it 35s to be safe.
Sleep 35s
