import os
import sys

# Make `import server` work — server.py lives in the router-hands dir (the parent of tests/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
