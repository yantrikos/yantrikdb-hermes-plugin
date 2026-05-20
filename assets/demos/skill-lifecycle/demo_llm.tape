# LLM-driven skill-lifecycle demo for yantrikdb-hermes-plugin.
#
# Render: vhs demo_llm.tape  (produces demo_llm.gif + demo_llm.mp4)
# Requires: vhs >= v0.10, python3, yantrikdb + yantrikdb-hermes-plugin +
#           openai installed, OPENAI_API_KEY in env.
#
# Tested on Linux (Ubuntu via WSL works). Windows VHS v0.11 has known
# shell-detection issues — use WSL on Windows.

Output demo_llm.gif
Output demo_llm.mp4

Set FontSize 13
Set Width 1280
Set Height 800
Set Padding 12
Set Theme "Catppuccin Mocha"

Hide
Type "cd /mnt/c/Users/sync/codes/yantrikdb-hermes-plugin && clear"
Enter
Sleep 200ms
Show

Type "python3 assets/demos/skill-lifecycle/demo_llm.py"
Sleep 600ms
Enter

# demo_llm.py: ~10s for the OpenAI tool-call loop end-to-end.
Sleep 20s
