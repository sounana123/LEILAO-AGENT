import os
from pathlib import Path

# Create __init__ files
init_files = [
    'agents/__init__.py',
    'agents/leilao/__init__.py',
]

for f in init_files:
    path = Path(f)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    print(f'Created {f}')
