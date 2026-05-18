"""Run ralph loop with proper Unicode handling for Windows."""
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from ralph_loop.cli import main
sys.exit(main(["run", "--project-root", r"C:\Users\lawsnic\OneDrive - amazon.com\Documents\HCLS-ai-book"]))
