"""Django's command-line utility for administrative tasks.

The settings module is *forced* here rather than merely defaulted. Both projects
are Django apps that often share one virtual environment, and a
``DJANGO_SETTINGS_MODULE`` left over in the shell (or exported for the UI
project) would otherwise be honoured silently: this service would boot with the
frontend's settings, serve no ``/api/v1/`` routes and answer 404 to everything.
An override is still honoured when it clearly belongs to this project, so the
test settings keep working.
"""
import os
import sys

PROJECT_PREFIX = "Artisan_platform."
DEFAULT_SETTINGS = "Artisan_platform.api_settings"


def _warn_if_projects_were_merged():
    """Catch the classic setup mistake before it shows up as a wall of 404s.

    If the standalone UI project was unpacked into this folder, its ``frontend``
    package and its own ``manage.py`` sit next to the API's apps. Everything
    then imports happily and the API quietly stops serving ``/api/v1/``.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(here, "frontend")):
        sys.stderr.write(
            "\n"
            "  *** The standalone UI project appears to be unpacked INSIDE the API ***\n"
            "  *** project (found a 'frontend/' package next to manage.py).        ***\n"
            "  *** Unpack the two zips into two SEPARATE folders.                  ***\n"
            "  *** This folder must contain only the API.                          ***\n"
            "\n"
        )


def main():
    """Run administrative tasks."""
    current = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    if not current.startswith(PROJECT_PREFIX):
        if current:
            sys.stderr.write(
                "Ignoring DJANGO_SETTINGS_MODULE=%r for the API project; using %r.\n"
                % (current, DEFAULT_SETTINGS)
            )
        os.environ["DJANGO_SETTINGS_MODULE"] = DEFAULT_SETTINGS
    _warn_if_projects_were_merged()
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
