#!/usr/bin/env python
"""Management entry point for the standalone Artisan Market frontend."""
import os
import sys


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
