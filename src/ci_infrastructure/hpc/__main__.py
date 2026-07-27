"""Enable ``python -m ci_infrastructure.hpc`` to reach the click CLI."""

from __future__ import annotations

from .orchestrate import main

if __name__ == "__main__":
    main()
