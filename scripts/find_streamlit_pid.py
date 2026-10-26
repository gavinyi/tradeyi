import subprocess
proc = subprocess.run(
    ["netstat", "-ano"], capture_output=True, text=True,
    encoding="utf-8", errors="replace",
)
for line in proc.stdout.splitlines():
    if ":8501" in line and "LISTENING" in line:
        print("Streamlit line:", line)
