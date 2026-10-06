"""WSGI entry point.

Used by ``runserver`` in development and by any WSGI server in deployment.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
